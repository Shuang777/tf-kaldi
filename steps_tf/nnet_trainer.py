import shutil
import os
import time
import numpy as np
import tensorflow as tf
from subprocess import Popen,PIPE
import nnet
import math
import logging
from dnn import DNN
from bn import BN
from lstm import LSTM
from seq2class import SEQ2CLASS
from jointdnn import JOINTDNN

logger = logging.getLogger('__main__')
logger.setLevel(logging.INFO)

iter_logger = logging.getLogger(__name__)
iter_logger.setLevel(logging.INFO)

class NNTrainer(object):
  '''
  a class for a neural network that can be used together with Kaldi.
  session is initialized either by read() or by init_nnet().
  '''

  def __init__(self, nnet_conf, input_dim, output_dim, feature_conf, num_gpus = 1, 
               use_gpu = True, gpu_ids = '-1', summary_dir = None):
    ''' just some basic config for this trainer '''
    self.arch = nnet_conf['nnet_arch']

    #tensorflow related
    self.graph = None
    self.sess = None

    #feature related
    self.batch_size = feature_conf['batch_size']
    self.max_length = feature_conf.get('max_length', 0)
    self.jitter_window = feature_conf.get('jitter_window', 0)

    #nnet training & decoding
    self.buckets_tr = nnet_conf.get('buckets_tr', None)
    self.buckets = nnet_conf.get('buckets', None)

    # for learning rate schedule. None in default (means scheduler outside)
    # otherwise use prep_learning_rate
    self.global_step = None
    self.learning_rate = None

    #gpu related
    self.wait_gpu = True
    self.num_gpus = num_gpus
    self.use_gpu = use_gpu
    self.gpu_ids = gpu_ids

    #summary directory
    self.summary_dir = summary_dir

    if self.arch == 'dnn':
      self.model = DNN(input_dim, output_dim, self.batch_size, num_gpus)
    elif self.arch == 'bn':
      self.model = BN(input_dim, output_dim, self.batch_size, num_gpus)
    elif self.arch == 'lstm':
      self.model = LSTM(input_dim, output_dim, self.batch_size, self.max_length, num_gpus)
    elif self.arch == 'seq2class':
      self.model = SEQ2CLASS(input_dim, output_dim, self.batch_size, self.max_length, num_gpus,
                             buckets_tr = self.buckets_tr, buckets = self.buckets)
    elif self.arch == 'jointdnn':
      self.model = JOINTDNN(input_dim, output_dim, self.batch_size, self.max_length, num_gpus,
                            buckets_tr = self.buckets_tr, buckets = self.buckets,
                            mode = nnet_conf.get('mode', 'joint'))
    elif self.arch == 'jointdnn-sid':
      self.model = JOINTDNN(input_dim, output_dim, self.batch_size, self.max_length, num_gpus,
                            buckets_tr = self.buckets_tr, buckets = self.buckets, mode = 'sid')
    elif self.arch == 'jointdnn-asr':
      self.model = JOINTDNN(input_dim, output_dim, self.batch_size, self.max_length, num_gpus,
                            buckets_tr = self.buckets_tr, buckets = self.buckets, mode = 'asr')
    else:
      raise RuntimeError("arch type %s not supported", self.arch)
 

  def get_max_length(self):
    return self.max_length   


  def get_batch_size(self):
    return self.batch_size


  def get_buckets(self):
    return self.buckets


  def make_proto(self, nnet_conf, nnet_proto_file):
    self.model.make_proto(nnet_conf, nnet_proto_file)


  def edit_model(self, nnet_proto_file, action):
    if action == 'finetune-sid':
      self.model.finetune_sid(self.graph, nnet_proto_file)
      self.sess.run(self.model.get_init_additional_op())
    else:
      raise RuntimeError('action %s not supported' % action)
    
    if self.summary_dir is not None:
      self.summary_writer = tf.summary.FileWriter(self.summary_dir, self.graph)
      self.summary_writer.flush()


  def __exit__ (self):
    if self.sess is not None:
      self.sess.close()

  
  def read(self, filename):
    filename = filename.strip()

    first_session = True
    if self.sess is not None:
      self.sess.close()
      tf.reset_default_graph()
      first_session = False

    self.graph = tf.Graph()

    with self.graph.as_default():
      self.saver = tf.train.import_meta_graph(filename+'.meta')

    logger.info("reading model from %s" % filename)
    self.model.read_from_file(self.graph, self.use_gpu)

    if first_session:
      self.set_gpu()

    self.sess = tf.Session(graph=self.graph, config=tf.ConfigProto(allow_soft_placement=True))

    with self.graph.as_default():
      self.saver.restore(self.sess, filename)


  def write(self, filename):
    with self.graph.as_default():
      save_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)
      saver = tf.train.Saver(save_list, max_to_keep=20)
      saver.save(self.sess, filename)


  def set_gpu(self):
    if self.use_gpu and self.num_gpus != 0:
      if self.gpu_ids == '-1':
        p1 = Popen (['pick-gpu', str(self.num_gpus), self.gpu_ids], stdout=PIPE)
        gpu_ids = str(p1.stdout.read())

        if self.wait_gpu:
          logger.info("Waiting for gpus")
          while(gpu_ids == "-1"):
            time.sleep(5)
            p1 = Popen (['pick-gpu', str(self.num_gpus), self.gpu_id], stdout=PIPE)
            gpu_ids = str(p1.stdout.read())
        else:
          raise RuntimeError("Picking gpu failed")
      else:
        # change 1-indexed to 0-indexed
        gpu_ids = ','.join([str(int(x)-1) for x in self.gpu_ids.split(',')])  
        
      os.environ['CUDA_VISIBLE_DEVICES'] = gpu_ids
    else:
      os.environ['CUDA_VISIBLE_DEVICES'] = ''


  def init_nnet(self, nnet_proto_file, seed = 777):
    self.graph = tf.Graph()

    self.model.init(self.graph, nnet_proto_file, seed)

    self.set_gpu()
    assert self.sess == None
    self.sess = tf.Session(graph=self.graph, config=tf.ConfigProto(allow_soft_placement=True))
    self.sess.run(self.model.get_init_all_op())

    if self.summary_dir is not None:
      self.summary_writer = tf.summary.FileWriter(self.summary_dir, self.graph)
      self.summary_writer.flush()

    return


  def prep_learning_rate(self, initial_lr, decay_steps, decay_rate):
    
    with self.graph.as_default():
      self.global_step = tf.Variable(0, trainable=False)
      self.learning_rate = tf.train.exponential_decay(initial_lr, 
                                        global_step = self.global_step,
                                        decay_steps = decay_steps, 
                                        decay_rate = decay_rate)
      self.add_global = self.global_step.assign_add(1)
      step_initializer = tf.variables_initializer([self.global_step], name = 'step_initializer')
      self.sess.run(step_initializer)

     
  def init_training(self, optimizer_conf):
    assert self.graph is not None
    self.model.init_training(self.graph, optimizer_conf, self.learning_rate)
    self.sess.run(self.model.get_init_train_op())

 
  def iter_data(self, logfile, train_gen, params, validation_mode = False):
    if self.arch == 'jointdnn':   # not a good implementation here, but let's just use it
      return self.iter_data_joint(logfile, train_gen, params, validation_mode)
    else:
      return self.iter_data_single(logfile, train_gen, params, validation_mode)


  def iter_data_single(self, logfile, train_gen, params, validation_mode = False):
    '''Train/test one iteration; check if 'learning_rate' in params to specify test mode'''
    assert self.batch_size*self.num_gpus == train_gen.get_batch_size()

    fh = logging.FileHandler(logfile, mode = 'w')
    iter_logger.addHandler(fh)

    sum_avg_loss = 0
    sum_accs = 0
    count_steps = 0

    sum_counts = 0        # counts could be frames or utterances
    sum_acc_counts = 0

    start_time = time.time()

    while(True):

      feed_dict, has_data = self.model.prep_feed(train_gen, params)
                                                 
      if not has_data:   # no more data for training
        break

      if validation_mode:
        # validation mode
        loss = self.sess.run(self.model.get_loss(), feed_dict = feed_dict)
      elif self.global_step is None:
        # training mode: learning rate scheduler
        _, loss = self.sess.run([self.model.get_train_op(), self.model.get_loss()], feed_dict = feed_dict)
      else:
        # training mode: exponential decay
        _, loss, _ = self.sess.run([self.model.get_train_op(), self.model.get_loss(), self.add_global], feed_dict = feed_dict)

      sum_avg_loss += loss
      count_steps += 1

      batch_counts = train_gen.get_last_batch_counts()
      sum_counts += batch_counts

      duration = time.time() - start_time

      if validation_mode or count_steps % 1000 == 0 or count_steps == 1:
        acc = self.sess.run(self.model.get_eval_acc(), feed_dict = feed_dict)
        sum_accs += 1.0 * acc
        sum_acc_counts += 1.0 * train_gen.get_last_batch_counts()

        # Print status to stdout.
        if count_steps % 20 == 0 or count_steps == 1:
          message = "Step %5d: avg loss = %.6f on %6d %s (%.2f %s per sec), peek acc: %.2f%%" % \
                    (count_steps, sum_avg_loss / (count_steps*self.num_gpus), 
                    sum_counts, train_gen.count_units(), sum_counts / duration, 
                    train_gen.count_units(), 100.0*acc/train_gen.get_last_batch_counts())

          if not validation_mode and self.global_step is not None:
            current_lr = self.sess.run(self.learning_rate)
            message += " cur_lr %.6f" % current_lr

          iter_logger.info(message)

    # reset batch_generator because it might be used again
    train_gen.reset_batch()

    avg_loss = sum_avg_loss / (count_steps * self.num_gpus)
    if sum_acc_counts == 0:
      avg_acc = None
      avg_acc_str = str(avg_acc)
    else:
      avg_acc = sum_accs/sum_acc_counts
      avg_acc_str = "%.2f%%" % (100.0*avg_acc)

    iter_logger.info("Complete: avg loss = %.6f on %d %s (%.2f sec passed, %.2f %s per sec), peek acc: %s", 
                avg_loss, sum_counts, train_gen.count_units(), duration, 
                sum_counts / duration, train_gen.count_units(), avg_acc_str)

    iter_logger.removeHandler(fh)

    return avg_loss, avg_acc_str


  def iter_data_joint(self, logfile, train_gen, params, validation_mode = False):
    '''Train/test one iteration; '''
    assert self.batch_size*self.num_gpus == train_gen.get_batch_size()

    fh = logging.FileHandler(logfile, mode = 'w')
    iter_logger.addHandler(fh)

    sum_avg_loss = 0
    sum_avg_asr_loss = 0
    sum_avg_sid_loss = 0
    count_steps = 0           # pair to record losses

    sum_asr_accs = 0
    sum_asr_acc_counts = 0    # pair to record asr accuracy

    sum_sid_accs = 0
    sum_sid_acc_counts = 0    # pair to record sid accuracy

    sum_counts = 0            # to record progression speed

    start_time = time.time()

    while(True):

      feed_dict, has_data = self.model.prep_feed(train_gen, params)
                                                 
      if not has_data:   # no more data for training
        break

      if validation_mode:
        loss, asr_loss, sid_loss = self.sess.run(
                                            [ self.model.get_loss(),
                                              self.model.get_asr_loss(),
                                              self.model.get_sid_loss() ], feed_dict = feed_dict)
      elif self.global_step is None:
        # training mode: learning rate scheduler
        _, loss, asr_loss, sid_loss = self.sess.run(
                                            [ self.model.get_train_op(), 
                                              self.model.get_loss(),
                                              self.model.get_asr_loss(),
                                              self.model.get_sid_loss() ], feed_dict = feed_dict)
      else:
        # training mode: exponential decay
        _, loss, asr_loss, sid_loss,  _ = self.sess.run(
                                            [ self.model.get_train_op(), 
                                              self.model.get_loss(), 
                                              self.model.get_asr_loss(),
                                              self.model.get_sid_loss(),
                                              self.add_global ], feed_dict = feed_dict)

      sum_avg_loss += loss
      sum_avg_asr_loss += asr_loss
      sum_avg_sid_loss += sid_loss
      count_steps += 1

      batch_counts = train_gen.get_last_batch_utts()
      sum_counts += batch_counts

      duration = time.time() - start_time

      if validation_mode or count_steps % 20 == 0 or count_steps == 1:
        # we want to check accuracy
        asr_acc, sid_acc = self.sess.run(
                                [ self.model.get_asr_eval_acc(),
                                  self.model.get_sid_eval_acc() ], feed_dict = feed_dict)

        sum_asr_accs += 1.0 * asr_acc
        sum_asr_acc_counts += 1.0 * train_gen.get_last_batch_frames()
        
        sum_sid_accs += 1.0 * sid_acc
        sum_sid_acc_counts += 1.0 * train_gen.get_last_batch_utts()

        # Print status to stdout.
        if count_steps % 20 == 0 or count_steps == 1:
          message = "Step %5d: avg loss = %.6f (asr %.6f & sid %.6f) on %6d utts (%.2f utts per sec), peek asr acc: %.2f%% & sid acc: %.2f%%" % \
                    (count_steps, sum_avg_loss / (count_steps*self.num_gpus),
                    sum_avg_asr_loss / (count_steps * self.num_gpus),
                    sum_avg_sid_loss / (count_steps * self.num_gpus), 
                    sum_counts, sum_counts / duration,  
                    100.0*asr_acc/train_gen.get_last_batch_frames(),
                    100.0*sid_acc/train_gen.get_last_batch_utts())

          if not validation_mode and self.global_step is not None:
            current_lr = self.sess.run(self.learning_rate)
            message += " cur_lr %.6f" % current_lr

          iter_logger.info(message)

    # reset batch_generator because it might be used again
    train_gen.reset_batch()

    avg_loss = sum_avg_loss / (count_steps * self.num_gpus)
    avg_asr_loss = sum_avg_asr_loss / (count_steps * self.num_gpus)
    avg_sid_loss = sum_avg_sid_loss / (count_steps * self.num_gpus)

    if sum_asr_acc_counts == 0:
      avg_acc = None
      avg_acc_str = str(avg_acc)
    else:
      avg_asr_acc = sum_asr_accs/sum_asr_acc_counts
      avg_asr_acc_str = "%.2f%%" % (100.0*avg_asr_acc)
      avg_sid_acc = sum_sid_accs/sum_sid_acc_counts
      avg_sid_acc_str = "%.2f%%" % (100.0*avg_sid_acc)
      avg_acc_str = "asr " + avg_asr_acc_str + " sid " + avg_sid_acc_str

    iter_logger.info("Complete: avg loss = %.6f (asr %.6f & sid %.6f) on %d %s (%.2f sec passed, %.2f %s per sec), peek acc: %s", 
                avg_loss, avg_asr_loss, avg_sid_loss, sum_counts, train_gen.count_units(), duration, 
                sum_counts / duration, train_gen.count_units(), avg_acc_str)

    iter_logger.removeHandler(fh)

    return avg_loss, avg_acc_str


  def get_current_lr(self):
    assert self.sess is not None
    return self.sess.run(self.learning_rate)


  def patch_to_batches(self, feats):
    ''' patch data so that it matches our batch_size'''
    if len(feats) % self.batch_size == 0:
      return feats
    row2pad = self.batch_size - (len(feats) % self.batch_size)
    feat_dim = len(feats[0])
    feats_padded = np.vstack([feats, np.zeros([row2pad, feat_dim])])
    return feats_padded
 
 
  def pack_utterance(self, feats, max_length = None):
    '''
    args:
      feats: list of array, i.e. matrix of size [num_frames, feat_dim]
    output:
      feat_packs: np 3-d array of size [num_batches, max_length, feat_dim]
      seq_length: np array of size [num_batches]
    '''
    batch_size = self.batch_size
    max_length = self.max_length if max_length else None
    jitter_window = self.jitter_window
    start_index = 0
    feats_packed = []
    seq_length = []
    post_pick = []
    pick_start = 0
    pick_end = (max_length + jitter_window) // 2
    while start_index + max_length < len(feats):
      end_index = start_index + max_length
      feats_packed.append(feats[start_index:end_index])
      seq_length.append(max_length)
      post_pick.append([pick_start, pick_end])
      # only the first window starts from 0, all others start from (max_length - jittter_window) / 2
      pick_start = (max_length - jitter_window) // 2      
      start_index += jitter_window

    num_zero = max_length + start_index - len(feats)
    zeros2pad = np.zeros((num_zero, len(feats[0])))
    feats_packed.append(np.concatenate((feats[start_index:], zeros2pad)))
    seq_length.append(len(feats) - start_index)
    # our last window goes till the end of the utterance
    post_pick.append([pick_start, len(feats) - start_index])

    
    # now we need to pad more zeros to fit the place holder, because each place holder can only host [ batch_size x max_length x feat_dim ] this many data
    batches2pad = batch_size - len(feats_packed) % batch_size
    if batches2pad != 0:
      zeros2pad = np.zeros((max_length, len(feats[0])))
      for i in range(batches2pad):
        feats_packed.append(zeros2pad)
        seq_length.append(0)
        post_pick.append([0, 0])

    feats_packed = np.array(feats_packed)
    seq_length = np.array(seq_length)

    return feats_packed, seq_length, post_pick


  def pack_utterance_jointdnn(self, feats, max_length = None):
    '''
    args:
      feats: list of array, i.e. matrix of size [num_frames, feat_dim]
    output:
      feat_packs: np 3-d array of size [num_batches, max_length, feat_dim]
    '''
    if max_length is None:
      max_length = self.max_length
    start_index = 0
    feats_packed = []
    while start_index + max_length < len(feats):
      end_index = start_index + max_length
      feats_packed.append(feats[start_index:end_index])
      start_index += max_length

    num_zero = max_length + start_index - len(feats)
    zeros2pad = np.zeros((num_zero, len(feats[0])))
    feats_packed.append(np.concatenate((feats[start_index:], zeros2pad)))
    
    # now we need to pad more zeros to fit the place holder, because each place holder can only host [ batch_size, max_length, feat_dim ] this many data
    batches2pad = self.batch_size - len(feats_packed) % self.batch_size
    if batches2pad != 0:
      zeros2pad = np.zeros((max_length, len(feats[0])))
      for i in range(batches2pad):
        feats_packed.append(zeros2pad)

    feats_packed = np.array(feats_packed)

    return feats_packed


  def predict(self, feats, no_softmax = False):
    if self.arch == 'dnn':
      posts = self.predict_dnn(feats, no_softmax)
    elif self.arch == 'bn':
      posts = self.gen_bn_feats(feats, no_softmax)
    elif self.arch == 'lstm':
      posts = self.predict_lstm(feats, no_softmax)
    elif self.arch in ['jointdnn', 'jointdnn-asr']:
      posts = self.predict_jointdnn(feats, no_softmax)
    else:
      raise RuntimeError("arch type %s not supported", self.arch)
    return posts


  def predict_dnn(self, feats, no_softmax = False):
    '''
    args: 
      feats: np 2-d array of size[num_frames, feat_dim]
    output:
      posts: np 2-d array of size[num_frames, num_targets]
    '''
    posts = []
    num_batches = int(math.ceil(1.0 * len(feats) / self.batch_size))
    for i in range(num_batches):
      batch_start = i*self.batch_size
      batch_end = (i+1)*self.batch_size
      # we avoid copying feats, only patch the last batch
      if len(feats) < batch_end:
        feats_padded = self.patch_to_batches(feats[batch_start:,])
      else:
        feats_padded = feats[batch_start:batch_end, :]
      
      feed_dict = self.model.prep_forward_feed(feats_padded)

      if no_softmax:
        batch_posts = self.sess.run(self.model.get_logits(), feed_dict=feed_dict)
      else:
        batch_posts = self.sess.run(self.model.get_outputs(), feed_dict=feed_dict)
      posts.append(batch_posts)

    posts = np.vstack(posts)

    return posts[0:len(feats),:]


  def gen_bn_feats(self):
    '''
    args: 
      feats: np 2-d array of size[num_frames, feat_dim]
    output:
      posts: np 2-d array of size[num_frames, num_targets]
    '''
    bn_outs = []
    num_batches = int(math.ceil(1.0 * len(feats) / self.batch_size))
    for i in range(num_batches):
      batch_start = i*self.batch_size
      batch_end = (i+1)*self.batch_size
      # we avoid copying feats, only patch the last batch
      if len(feats) < batch_end:
        feats_padded = self.patch_to_batches(feats[batch_start:,])
      else:
        feats_padded = feats[batch_start:batch_end, :]
      
      feed_dict = self.model.prep_forward_feed(feats_padded)

      batch_bn_outs = self.sess.run(self.model.get_bn(), feed_dict=feed_dict)
      bn_outs.append(batch_bn_outs)

    bn_outs = np.vstack(bn_outs)

    return bn_outs[0:len(feats),:]


  def predict_lstm(self, feats, no_softmax = False):
    '''
    we need a sliding window to generate frame posteriors
    args: 
      feats: np 2-d array of size[num_frames, feat_dim]
    output:
      posts: np 2-d array of size[num_frames, num_targets]
    '''
    batch_size = self.batch_size
    # we use a rolling window to process the whole utterance
    feats_packed, seq_length, post_pick = self.pack_utterance(feats, max_length = self.max_length)

    posts = []
    assert len(feats_packed) % batch_size == 0
    num_batches = len(feats_packed) / batch_size
    for i in range(num_batches):
      batch_start = i*batch_size
      batch_end = (i+1)*batch_size
      feats_batch = feats_packed[batch_start:batch_end, :]
      seq_length_batch = seq_length[batch_start:batch_end]

      feed_dict = self.model.prep_forward_feed(feats_batch, seq_length_batch, 1.0, 1.0)

      if no_softmax:
        batch_posts = self.sess.run(self.model.get_logits(), feed_dict=feed_dict)
      else:
        batch_posts = self.sess.run(self.model.get_outputs(), feed_dict=feed_dict)
      # batch_posts of size [batch_size, max_len, num_targets]

      for piece in range(batch_size):
        if post_pick[piece][0] != post_pick[piece][1]:
          # post_pick specifies the index of posterior to pick out to form decoding sequence
          posts.append(batch_posts[piece, post_pick[piece][0]:post_pick[piece][1]])

    posts = np.concatenate(posts)

    return posts[0:len(feats),:]

  
  def predict_jointdnn(self, feats, no_softmax = False):
    '''
    we need to pack utterance to batches to generate frame posteriors
    args: 
      feats: np 2-d array of size[num_frames, feat_dim]
    output:
      posts: np 2-d array of size[num_frames, num_targets]
    '''
    # we use a rolling window to process the whole utterance
    feats_packed = self.pack_utterance_jointdnn(feats, 
                        max_length = self.buckets_tr[0])

    posts = []
    assert len(feats_packed) % self.batch_size == 0
    num_batches = len(feats_packed) / self.batch_size
    for i in range(num_batches):
      batch_start = i*self.batch_size
      batch_end = (i+1)*self.batch_size
      # feats_batch of size [batch_size, max_len, feat_dim]
      feats_batch = feats_packed[batch_start:batch_end, :]

      feed_dict = self.model.prep_forward_feed(feats_batch)

      # batch_posts of size [batch_size, max_len, num_targets]
      if no_softmax:
        batch_posts = self.sess.run(self.model.get_asr_logits(), feed_dict=feed_dict)
      else:
        batch_posts = self.sess.run(self.model.get_asr_outputs(), feed_dict=feed_dict)

      posts.append(np.vstack(batch_posts))

    posts = np.concatenate(posts)

    return posts[0:len(feats),:]


  def gen_embedding(self, feats, mask, bucket_id = 0, embedding_index = 0):
    if self.arch == 'seq2class':
      feed_dict, embedding_layer = self.model.prep_forward_feed(feats, mask, 
                                                                bucket_id, embedding_index)
      embedding = self.sess.run(embedding_layer, feed_dict=feed_dict)
    elif self.arch in ['jointdnn', 'jointdnn-sid']:
      feed_dict, embedding_layer = self.model.prep_forward_sid(feats, mask, 
                                                               bucket_id, embedding_index)
      embedding = self.sess.run(embedding_layer, feed_dict=feed_dict)
    else:
      raise RuntimeError("arch type %s not supported", self.arch)
    return embedding


  def get_bucket_id(self, length):
    # return the smallest possible bucket that fits length
    bucket_id = 0
    while bucket_id < len(self.buckets)-1 and length > self.buckets[bucket_id]:
      bucket_id += 1
    return bucket_id


  def gen_utt_embedding(self, feats, embedding_index = 0):
    bucket_id = self.get_bucket_id(len(feats))
    bucket_size = self.buckets[bucket_id]
    start_index = 0
    embedding_acc = None
    embedding_count = 0

    mask = np.ones(bucket_size)
    while start_index + bucket_size <= len(feats):
      end_index = start_index + bucket_size
      bucket_feats = feats[start_index:end_index]
      embedding = self.gen_embedding(bucket_feats, mask, bucket_id, embedding_index)
      if embedding_acc is None:
        embedding_acc = embedding
      else:
        embedding_acc += embedding
      embedding_count += 1
      start_index = end_index
      
    # last one, we shift back a bit, if possible
    if start_index < len(feats):
      start_index = max(0, len(feats) - bucket_size)
      num_zeros = start_index + bucket_size - len(feats)
      zeros2pad = np.zeros((num_zeros, len(feats[0])))
      if num_zeros == 0:
        bucket_feats = feats[start_index:]
      else:
        bucket_feats = np.concatenate((feats[start_index:],zeros2pad))
        mask = np.append(np.ones(len(feats)-start_index), np.zeros(num_zeros))
      embedding = self.gen_embedding(bucket_feats, mask, bucket_id, embedding_index)
      if embedding_acc is None:
        embedding_acc = embedding
      else:
        embedding_acc += embedding
      embedding_count += 1

    embedding = embedding_acc[0] / embedding_count
    return embedding

