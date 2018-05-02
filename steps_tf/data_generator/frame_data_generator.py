from subprocess import Popen, PIPE, check_output
import tempfile
import kaldi_io
import kaldi_IO
import pickle
import shutil
import numpy
import os
import math

DEVNULL = open(os.devnull, 'w')

class FrameDataGenerator:
  def __init__ (self, data, labels, trans_dir, exp, name, conf, 
                seed=777, shuffle=False, loop=False, num_gpus = 1):
    
    self.data = data
    self.labels = labels
    self.exp = exp
    self.name = name
    self.batch_size = conf.get('batch_size', 256) * num_gpus
    self.splice = conf.get('context_width', 5)
    self.feat_type = conf.get('feat_type', 'raw')
    self.delta_opts = conf.get('delta_opts', '')
    self.max_split_data_size = 5000 ## These many utterances are loaded into memory at once.

    self.loop = loop    # keep looping over dataset

    self.tmp_dir = tempfile.mkdtemp(prefix = conf.get('tmp_dir', '/data/suhang/exp/tmp/'))

    ## Read number of utterances
    with open (data + '/utt2spk') as f:
      self.num_utts = sum(1 for line in f)

    cmd = "cat %s/feats.scp | utils/shuffle_list.pl --srand %d > %s/shuffle.%s.scp" % (data, seed, exp, self.name)
    Popen(cmd, shell=True).communicate()

    # prepare feature pipeline
    if conf.get('cmvn_type', 'utt') == 'utt':
      cmd = ['apply-cmvn', '--utt2spk=ark:' + self.data + '/utt2spk',
                 'scp:' + self.data + '/cmvn.scp',
                 'scp:' + exp + '/shuffle.' + self.name + '.scp','ark:- |']
    elif conf['cmvn_type'] == 'sliding':
      cmd = ['apply-cmvn-sliding', '--norm-vars=false', '--center=true', '--cmn-window=300', 
              'scp:' + exp + '/shuffle.' + self.name + '.scp','ark:- |']
    else:
      raise RuntimeError("cmvn_type %s not supported" % conf['cmvn_type'])

    if self.feat_type == 'delta':
      feat_dim_delta_multiple = 3
    else:
      feat_dim_delta_multiple = 1
    
    if self.feat_type == 'delta':
      cmd.extend(['add-deltas', self.delta_opts, 'ark:-', 'ark:- |'])
    elif self.feat_type in ['lda', 'fmllr']:
      cmd.extend(['splice-feats', 'ark:-','ark:- |'])
      cmd.extend(['transform-feats', exp+'/final.mat', 'ark:-', 'ark:- |'])

    if self.feat_type == 'fmllr':
      assert os.path.exists(trans_dir+'/trans.1') 
      cmd.extend(['transform-feats','--utt2spk=ark:' + self.data + '/utt2spk',
              '\'ark:cat %s/trans.* |\'' % trans_dir, 'ark:-', 'ark:-|'])
    
    cmd.extend(['copy-feats', 'ark:-', 'ark,scp:'+self.tmp_dir+'/shuffle.'+self.name+'.ark,'+exp+'/'+self.name+'.scp'])
    Popen(' '.join(cmd), shell=True).communicate()

    if name == 'train':
      cmd =['splice-feats', '--left-context='+str(self.splice), '--right-context='+str(self.splice),
            '\'scp:head -10000 %s/%s.scp |\'' % (exp, self.name), 'ark:- |', 'compute-cmvn-stats', 
            'ark:-', exp+'/cmvn.mat']
      Popen(' '.join(cmd), shell=True).communicate()

    self.num_split = int(math.ceil(1.0 * self.num_utts / self.max_split_data_size))
    for i in range(self.num_split):
      split_scp_cmd = 'utils/split_scp.pl -j %d ' % (self.num_split)
      split_scp_cmd += '%d %s/%s.scp %s/split.%s.%d.scp' % (i, exp, self.name, self.tmp_dir, self.name, i)
      Popen (split_scp_cmd, shell=True).communicate()
    
    numpy.random.seed(seed)

    self.feat_dim = int(check_output(['feat-to-dim', 'scp:%s/%s.scp' %(exp, self.name), '-'])) * \
                    feat_dim_delta_multiple * (2*self.splice+1)
    self.split_data_counter = 0
    
    self.x = numpy.empty ((0, self.feat_dim))
    self.y = numpy.empty (0, dtype='int32')
    
    self.batch_pointer = 0


  def get_feat_dim(self):
    return self.feat_dim


  def clean (self):
    shutil.rmtree(self.tmp_dir)


  def has_data(self):
  # has enough data for next batch
    if self.loop or self.split_data_counter != self.num_split:     # we always have data if in loop mode
      return True
    elif self.batch_pointer + self.batch_size >= len(self.x):
      return False
    return True
      

  ## Return a batch to work on
  def get_next_split_data (self):
    '''
    output: 
      feat_list: list of np matrix [num_frames, feat_dim]
      label_list: list of int32 np array [num_frames] 
    '''

    p1 = Popen (['splice-feats', '--print-args=false', '--left-context='+str(self.splice),
                 '--right-context='+str(self.splice), 
                 'scp:'+self.tmp_dir+'/split.'+self.name+'.'+str(self.split_data_counter)+'.scp',
                 'ark:-'], stdout=PIPE, stderr=DEVNULL)
    p2 = Popen (['apply-cmvn', '--print-args=false', '--norm-vars=true', self.exp+'/cmvn.mat',
                 'ark:-', 'ark:-'], stdin=p1.stdout, stdout=PIPE, stderr=DEVNULL)

    feat_list = []
    label_list = []
    
    while True:
      uid, feat = kaldi_IO.read_utterance (p2.stdout)
      if uid == None:
        break;
      if uid in self.labels:
        feat_list.append (feat)
        label_list.append (self.labels[uid])

    p1.stdout.close()
    
    if len(feat_list) == 0 or len(label_list) == 0:
      raise RuntimeError("No feats are loaded! please check feature and labels, and make sure they are matched.")

    return (feat_list, label_list)

          
  ## Retrive a mini batch
  def get_batch_frames (self):
    '''
    output:
      x_mini: np matrix [num_frames, feat_dim]
      y_mini: np array [num_frames]
    '''
    # read split data until we have enough for this batch
    while (self.batch_pointer + self.batch_size >= len (self.x)):
      if not self.loop and self.split_data_counter == self.num_split:
        # not loop mode and we arrive the end, do not read anymore
        return None, None

      x,y = self.get_next_split_data()

      self.x = numpy.concatenate ((self.x[self.batch_pointer:], numpy.vstack(x)))
      self.y = numpy.append (self.y[self.batch_pointer:], numpy.hstack(y))
      self.batch_pointer = 0

      ## Shuffle data
      randomInd = numpy.array(range(len(self.x)))
      numpy.random.shuffle(randomInd)
      self.x = self.x[randomInd]
      self.y = self.y[randomInd]

      self.split_data_counter += 1
      if self.loop and self.split_data_counter == self.num_split:
        self.split_data_counter = 0
    
    x_mini = self.x[self.batch_pointer:self.batch_pointer+self.batch_size]
    y_mini = self.y[self.batch_pointer:self.batch_pointer+self.batch_size]
    
    self.batch_pointer += self.batch_size
    self.last_batch_frames = len(y_mini)

    return x_mini, y_mini

  
  def get_batch_size(self):
    return self.batch_size


  def get_last_batch_counts(self):
    return self.last_batch_frames

  
  def count_units(self):
    return 'frames'


  def reset_batch(self):
    self.split_data_counter = 0


  def save_target_counts(self, num_targets, output_file):
    # here I'm assuming training data is less than 10,000 hours
    counts = numpy.zeros(num_targets, dtype='int64')
    for alignment in self.labels.values():
      counts += numpy.bincount(alignment, minlength = num_targets)
    # add a ``half-frame'' to all the elements to avoid zero-counts (decoding issue)
    counts = counts.astype(float) + 0.5
    numpy.savetxt(output_file, counts, fmt = '%.1f')

