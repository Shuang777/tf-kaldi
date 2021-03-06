from subprocess import Popen, PIPE, check_output
import tempfile
import kaldi_io
import kaldi_IO
import pickle
import shutil
import numpy
import random
import os
import math

DEVNULL = open(os.devnull, 'w')

class JointDNNDataGenerator:
  def __init__ (self, data, sid_labels, asr_labels, exp, name, conf, 
                seed=777, shuffle=False, num_gpus = 1, buckets=None):
    
    self.data = data
    self.sid_labels = sid_labels
    self.asr_labels = asr_labels
    self.exp = exp
    self.name = name
    self.batch_size = conf.get('batch_size', 256) * num_gpus
    self.splice = conf.get('context_width', 5)
    self.sliding_window = conf.get('sliding_window', 200)
    self.max_length = conf.get('max_length', 400)
    self.feat_type = conf.get('feat_type', 'raw')
    self.fit_buckets = conf.get('fit_buckets', True)
    self.clean_up = conf.get('clean_up', True)
    self.buckets = buckets

    if self.name == 'train':
      self.loop = conf.get('loop_mode', False)
      self.split_per_iter = conf.get('split_per_iter', None)
    else:
      self.loop = False
      self.split_per_iter = None
    self.split_counter = 0        # keep increasing
    self.split_data_counter = 0   # loop from 0 to num_split
    if self.loop and self.split_per_iter is None:
      raise RuntimeError('Must specify split_per_iter in loop mode!')
    
    self.tmp_dir = tempfile.mkdtemp(prefix = conf.get('tmp_dir', '/data/suhang/exp/tmp'))

    if self.buckets is None:    # we only have one bucket in this case
      self.buckets = [self.max_length]

    ## Read number of utterances
    with open (data + '/feats.%s.scp' % self.name) as f:
      self.num_utts = sum(1 for line in f)

    shutil.copyfile("%s/feats.%s.scp" % (self.data, self.name), "%s/%s.scp" % (self.exp, self.name))

    if name == 'train':
      cmd = ['copy-feats', '\'scp:head -10000 %s/%s.scp |\'' % (exp, self.name), 'ark:- |']

      cmd.extend(['splice-feats', '--left-context='+str(self.splice), 
                  '--right-context='+str(self.splice), 'ark:- ark:- |'])
      
      cmd.extend(['compute-cmvn-stats', 'ark:-', exp+'/cmvn.mat'])

      Popen(' '.join(cmd), shell=True).communicate()

    self.num_split = int(open('%s/num_split.%s' % (self.data, self.name)).read())

    for i in range(self.num_split):
      shutil.copyfile("%s/feats.%s.%d.scp" % (self.data, self.name, (i+1)), "%s/split.%s.%d.scp" % (self.tmp_dir, self.name, i))

    numpy.random.seed(seed)

    self.feat_dim = int(open('%s/feat_dim' % self.data).read()) * (2*self.splice+1)
    
    self.x = numpy.empty ((0, self.max_length, self.feat_dim))  # features
    self.y = numpy.empty ((0, self.max_length), dtype='int32')  # asr_labels
    self.z = numpy.empty (0, dtype='int32')                     # sid_labels
    self.mask = numpy.empty ((0, self.max_length), dtype='float32')

    self.batch_pointer = 0

    
  def get_feat_dim(self):
    return self.feat_dim


  def __del__(self):
    if self.clean_up and os.path.exists(self.tmp_dir):
      shutil.rmtree(self.tmp_dir)


  def has_data(self):
    # has enough data for next batch
    if self.batch_pointer + self.batch_size > len(self.x):
      if self.loop and (self.split_counter+1) % self.split_per_iter == 0:
        return False
      if not self.loop and self.split_data_counter == self.num_split:
        return False
    return True
     
      
  def get_num_split(self):
    return self.num_split 


  def get_num_batches(self):
    return self.num_samples / self.batch_size


  ## Return a batch to work on
  def get_next_split_data (self):
    '''
    output: 
      feat_list: list of np matrix [num_frames, feat_dim]
      asr_label_list: list of int32 np array [num_frames] 
      sid_label_list: list of int32
    '''
    p1 = Popen (['splice-feats', '--print-args=false', '--left-context='+str(self.splice),
                 '--right-context='+str(self.splice), 
                 'scp:'+self.tmp_dir+'/split.'+self.name+'.'+str(self.split_data_counter)+'.scp',
                 'ark:-'], stdout=PIPE, stderr=DEVNULL)
    p2 = Popen (['apply-cmvn', '--print-args=false', '--norm-vars=true', self.exp+'/cmvn.mat',
                 'ark:-', 'ark:-'], stdin=p1.stdout, stdout=PIPE, stderr=DEVNULL)

    feat_list = []
    asr_label_list = []
    sid_label_list = []
    
    while True:
      uid, feat = kaldi_IO.read_utterance (p2.stdout)
      if uid == None:
        break;
      if uid in self.asr_labels and uid in self.sid_labels:
        feat_list.append (feat)
        asr_label_list.append (self.asr_labels[uid])
        sid_label_list.append (self.sid_labels[uid])

    p2.stdout.close()
    
    if len(feat_list) == 0 or len(asr_label_list) == 0:
      raise RuntimeError("No feats are loaded! please check feature and labels, and make sure they are matched.")

    return (feat_list, asr_label_list, sid_label_list)


  def pack_utt_data(self, features, asr_labels, sid_labels, bucket_id):
    '''
    for each utterance, we use a rolling window to generate enough segments for speaker ID modeling
    input:
      features: list of np 2d-array [num_frames, feat_dim]
      asr_labels: list of np array [num_frames]
      sid_labels: list of int32
    output:
      features_packed: np 3d-array [batch_size, max_length, feat_dim]
      asr_labels_packed: matrix[batch_size, max_length]
      sid_labels_packed: array[batch_size]
    '''
    assert len(features) == len(asr_labels)

    features_packed = []
    asr_labels_packed = []
    sid_labels_packed = []
    mask = []
    max_length = self.buckets[bucket_id]
    sliding_window = self.sliding_window

    for feat, asr_lab, sid_lab in zip(features, asr_labels, sid_labels):

      assert len(feat) == len(asr_lab)
      start_index = 0

      while start_index + max_length < len(feat):
        # cut utterances into pieces
        end_index = start_index + max_length
        features_packed.append(feat[start_index:end_index])
        asr_labels_packed.append(asr_lab[start_index:end_index])
        mask.append(numpy.ones(max_length))
        sid_labels_packed.append(sid_lab)
        start_index += sliding_window

      # last part, if long enough (bigger than max_length/2), pad the features
      if len(feat) - start_index >= sliding_window:
        num_zero = max_length + start_index - len(feat)
        zeros2pad = numpy.zeros((num_zero, len(feat[0])))
        features_packed.append(numpy.concatenate((feat[start_index:], zeros2pad)))
        asr_labels_packed.append(numpy.append(asr_lab[start_index:], 
                                              numpy.zeros(num_zero,dtype='int32')))
        mask.append(numpy.append(numpy.ones(len(feat)-start_index), numpy.zeros(num_zero)))
        sid_labels_packed.append(sid_lab)

    features_packed = numpy.array(features_packed)
    mask = numpy.array(mask)
    asr_labels_packed = numpy.array(asr_labels_packed)
    sid_labels_packed = numpy.array(sid_labels_packed)

    return features_packed, asr_labels_packed, sid_labels_packed, mask


  def get_batch_utterances (self):
    '''
    output:
      x_mini: np matrix [batch_size, max_length, feat_dim]
      y_mini: np matrix [batch_size]
      mask: np matrix [batch_size, max_length]
    '''
    # read split data until we have enough for this batch
    while (self.batch_pointer + self.batch_size > len(self.x)):
      if not self.has_data():
        # let's just throw away the last few samples
        return None, None, None, None, 0

      feats, asr_labels, sid_labels = self.get_next_split_data()

      # pick a random bucket to prepare the data
      bucket_id = numpy.random.randint(0, len(self.buckets))
      x_packed, y_packed, z_packed, mask_packed = self.pack_utt_data(feats, asr_labels, 
                                                          sid_labels, bucket_id)
      
      self.bucket_id = bucket_id

      # We just throw away data left, and shuffle data, utterance base
      randomInd = numpy.array(range(len(x_packed)))
      numpy.random.shuffle(randomInd)
      self.x = x_packed[randomInd]
      self.y = y_packed[randomInd]
      self.z = z_packed[randomInd]
      self.mask = mask_packed[randomInd]
      
      self.batch_pointer = 0

      self.split_counter += 1
      self.split_data_counter += 1
      if self.loop and self.split_data_counter == self.num_split:
        self.split_data_counter = 0
    
    x_mini = self.x[self.batch_pointer:self.batch_pointer+self.batch_size]
    y_mini = self.y[self.batch_pointer:self.batch_pointer+self.batch_size]
    z_mini = self.z[self.batch_pointer:self.batch_pointer+self.batch_size]
    mask_mini = self.mask[self.batch_pointer:self.batch_pointer+self.batch_size]

    self.last_batch_utts = len(y_mini)
    self.last_batch_frames = mask_mini.sum()
    self.batch_pointer += self.batch_size

    return x_mini, y_mini, z_mini, mask_mini, self.bucket_id


  def get_batch_size(self):
    return self.batch_size

  def get_last_batch_counts(self):
    # TODO: this is a temporary solution; not suitable for sid finetuning
    return self.get_last_batch_frames()

  def get_last_batch_utts(self):
    return self.last_batch_utts

  def get_last_batch_frames(self):
    return self.last_batch_frames

  def reset_batch(self):
    if self.loop:
      self.split_counter += 1
    else:
      self.split_data_counter = 0

  def count_units(self):
    return 'utterances'

  def save_target_counts(self, num_targets, output_file):
    # here I'm assuming training data is less than 10,000 hours
    counts = numpy.zeros(num_targets, dtype='int64')
    for alignment in self.asr_labels.values():
      counts += numpy.bincount(alignment, minlength = num_targets)
    # add a ``half-frame'' to all the elements to avoid zero-counts (decoding issue)
    counts = counts.astype(float) + 0.5
    numpy.savetxt(output_file, counts, fmt = '%.1f')

