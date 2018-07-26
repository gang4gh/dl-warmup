# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""sequence-to-Sequence with attention model"""

import math
import logging
import numpy as np
import tensorflow as tf

from collections import namedtuple
HParams = namedtuple('HParams',
                     'mode batch_size '
                     'enc_layers enc_timesteps dec_timesteps '
                     'num_hidden emb_dim '
                     'beam_size init_dec_state adam_epsilon decay_scale FLAGS')


class StackedLayer(tf.layers.Layer):
  """Stack multiple tf.layers.Layers"""
  def __init__(self, layers, **kwargs):
    super(StackedLayer, self).__init__(**kwargs)
    self.layers = layers

  def build(self, input_shape):
    for l in self.layers:
      l.build(input_shape)
      input_shape = l.compute_output_shape(input_shape)
    self.built = True

  def call(self, inputs):
    for l in self.layers:
      inputs = l.call(inputs)
    return inputs

  def compute_output_shape(self, input_shape):
    for l in self.layers:
      input_shape = l.compute_output_shape(input_shape)
    return input_shape

class Seq2SeqAttentionModel(object):
  """Wrapper for Tensorflow model graph for text sum vectors."""

  def __init__(self, hps, vocab):
    self._hps = hps
    self._vocab = vocab

  def run_train_step(self, sess):
    to_return = [self._train_op, self._summaries, self._loss, self.global_step]
    return sess.run(to_return)

  def run_infer_step(self, sess):
    to_return = [self._predicted_ids, self._article_strings, self._summary_strings]
    return sess.run(to_return)

  def read_inputs(self, sess):
    """read inputs from dataset for naive baseline or test purpose.
    returns:
      article_strings: string of [batch_size]
      summary_strings: string of [batch_size]
      articles: int32 of [batch_size, max_encoding_len]
      targets: int32 of [batch_size, max_decoding_len + 1]
    """
    to_return = [self._article_strings, self._summary_strings, self._articles, self._targets]
    return sess.run(to_return)

  def initialize_dataset(self, sess, data_filepath):
    logging.info('initialize dataset data_filepath = %s', data_filepath)
    sess.run(self._iterator.initializer,
             feed_dict = {self._data_filepath: data_filepath})

  def _setup_model_input(self):
    hps = self._hps
    pad_id = self._vocab.token_pad_id
    start_id = self._vocab.token_start_id
    end_id = self._vocab.token_end_id
    eos_id = self._vocab.token_eos_id

    def _parse_line(line):
      article_ids, _, article_text, summary_ids, _, summary_text = self._vocab.parse_article(line.decode(), hps.FLAGS.focus_sentence_id)
      article_len = len(article_ids)
      if article_len < hps.enc_timesteps:
        article_ids = article_ids + [pad_id] * (hps.enc_timesteps - article_len)
      else:
        article_ids = article_ids[:hps.enc_timesteps]
        article_len = hps.enc_timesteps

      summary_len = len(summary_ids)
      if summary_len <= hps.dec_timesteps - 1:
        summary_ids = [start_id] + summary_ids + [end_id] * (hps.dec_timesteps - summary_len)
        summary_len += 1
      else:
        summary_ids = [start_id] + summary_ids[:hps.dec_timesteps]
        summary_len = hps.dec_timesteps

      summary_sentence_ids = []
      for ind, val in enumerate(summary_ids):
        if ind == 0 or val == eos_id:
          summary_sentence_ids.append([])
          continue
        if val == end_id: break
        summary_sentence_ids[-1].append(val)
      if len(summary_sentence_ids) < hps.FLAGS.model_max_sentence_count:
        summary_sentence_ids = summary_sentence_ids + [[] for _ in range(hps.FLAGS.model_max_sentence_count - len(summary_sentence_ids))]
      else:
        summary_sentence_ids = summary_sentence_ids[:hps.FLAGS.model_max_sentence_count]
      summary_sentence_count = max(ind for ind, val in enumerate(summary_sentence_ids) if len(val) > 0) + 1
      summary_sentence_lengths = []
      for ind in range(hps.FLAGS.model_max_sentence_count):
        ids = summary_sentence_ids[ind]
        length = len(ids)
        if length <= hps.FLAGS.model_max_sentence_length - 1:
          summary_sentence_ids[ind] = [start_id] + ids + [end_id] * (hps.FLAGS.model_max_sentence_length - length)
          summary_sentence_lengths.append(length + 1)
          #summary_sentence_lengths.append(length + 1 if length > 0 else 0)
        else:
          summary_sentence_ids[ind] = [start_id] + ids[:hps.FLAGS.model_max_sentence_length]
          summary_sentence_lengths.append(hps.FLAGS.model_max_sentence_length)

      return (
          np.array(article_ids, np.int32),
          np.int32(article_len),
          np.array(summary_ids, np.int32),
          np.int32(summary_len),
          article_text,
          summary_text,
          np.int32(summary_sentence_count),
          np.array(summary_sentence_lengths, np.int32),
          np.array(summary_sentence_ids, np.int32),
          )

    def fix_shapes(article_ids, article_len, summary_ids, summary_len, article_text, summary_text, ss_count, ss_lengths, ss_ids):
      article_ids.set_shape([hps.enc_timesteps])
      summary_ids.set_shape([hps.dec_timesteps + 1])
      article_len.set_shape([])
      summary_len.set_shape([])
      article_text.set_shape([])
      summary_text.set_shape([])
      ss_count.set_shape([])
      ss_lengths.set_shape([hps.FLAGS.model_max_sentence_count])
      ss_ids.set_shape([hps.FLAGS.model_max_sentence_count, hps.FLAGS.model_max_sentence_length + 1])
      return article_ids, summary_ids, article_len, summary_len, article_text, summary_text, ss_count, ss_lengths, ss_ids

    self._data_filepath = tf.placeholder(tf.string, shape=[])
    dataset = tf.data.TextLineDataset(self._data_filepath)
    dataset = dataset.map(lambda line: tf.py_func(_parse_line, [line], [tf.int32, tf.int32, tf.int32, tf.int32, tf.string, tf.string, tf.int32, tf.int32, tf.int32], stateful=False))
    dataset = dataset.map(fix_shapes)
    if hps.mode != 'decode' and hps.mode != 'naive':
      dataset = dataset.repeat()
    dataset = dataset.apply(tf.contrib.data.batch_and_drop_remainder(hps.batch_size))
    dataset = dataset.prefetch(4)
    logging.debug('dataset shape: %s', dataset)
    self._iterator = dataset.make_initializable_iterator()

    iterator_state = tf.contrib.data.make_saveable_from_iterator(self._iterator)
    tf.add_to_collection(tf.GraphKeys.SAVEABLE_OBJECTS, iterator_state)

    next_res = self._iterator.get_next()
    self._articles, self._targets, self._article_lens, self._abstract_lens, self._article_strings, self._summary_strings, self.ss_count, self.ss_lengths, self.ss_ids = next_res
    #self._articles = tf.reshape(self._articles, [hps.batch_size, hps.enc_timesteps])
    #self._targets = tf.reshape(self._targets, [hps.batch_size, hps.dec_timesteps + 1])
    #self._article_lens = tf.reshape(self._article_lens, [hps.batch_size])
    #self._abstract_lens = tf.reshape(self._abstract_lens, [hps.batch_size])

  def _add_seq2seq(self):
    hps = self._hps
    vsize = self._vocab.get_vocab_size()

    uniform_initializer = tf.random_uniform_initializer(-0.1, 0.1)

    with tf.variable_scope('seq2seq'):
      encoder_inputs = tf.transpose(self._articles)
      decoder_inputs = tf.transpose(self._targets[:, :-1])
      targets = tf.transpose(self._targets[:, 1:])
      loss_masks = tf.transpose(tf.sequence_mask(self._abstract_lens, hps.dec_timesteps, dtype=tf.float32))
      if hps.decay_scale > 1:
        decay_factor = math.exp(math.log(1 / hps.decay_scale) / (hps.dec_timesteps - 1))
        # weigths are normalized in sequence_loss, decay_base could be 1
        decay_base = hps.dec_timesteps * (1 - decay_factor) / (1 - decay_factor ** hps.dec_timesteps)
        decay_weights = tf.constant([[decay_base * decay_factor ** i] * hps.batch_size for i in range(hps.dec_timesteps)])
        loss_weights = loss_masks * decay_weights
        logging.info('apply decayed weights with decay_factor = %f, decay_base = %f', decay_factor, decay_base)
      else:
        loss_weights = loss_masks
      if hps.FLAGS.eos_scale > 1:
        loss_weights = tf.where(tf.equal(decoder_inputs, tf.constant(self._vocab.token_eos_id)), loss_weights * hps.FLAGS.eos_scale, loss_weights)
      #loss_weights = tf.Print(loss_weights, [decoder_inputs, self._summary_strings, self.ss_count, self.ss_lengths, self.ss_ids], summarize=300)
      article_lens = self._article_lens
      abstract_lens = self._abstract_lens

      # Embedding shared by the input and outputs.
      with tf.variable_scope('embedding'):
        embedding = tf.get_variable('embedding', [vsize, hps.emb_dim],
            initializer=tf.truncated_normal_initializer(stddev=1e-4))
        #emb_encoder_inputs = tf.nn.embedding_lookup(embedding, encoder_inputs)
        #emb_decoder_inputs = tf.nn.embedding_lookup(embedding, decoder_inputs)
        emb_encoder_inputs = tf.gather(embedding, encoder_inputs)
        emb_decoder_inputs = tf.gather(embedding, decoder_inputs)
 
      encoding_layer_inputs = emb_encoder_inputs
      for layer_i in range(hps.enc_layers):
        with tf.variable_scope('encoder%d' % layer_i):
          cell_fw = tf.contrib.rnn.LSTMCell(hps.num_hidden, initializer=uniform_initializer)
          cell_bw = tf.contrib.rnn.LSTMCell(hps.num_hidden, initializer=uniform_initializer)
          (rnn_outputs, (fw_state, bw_state)) = tf.nn.bidirectional_dynamic_rnn(
              cell_fw, cell_bw, encoding_layer_inputs,
              sequence_length=article_lens, dtype=tf.float32, time_major=True)
          encoding_layer_inputs = tf.concat([rnn_outputs[0], rnn_outputs[1], emb_encoder_inputs], 2)
      emb_memory = tf.transpose(encoding_layer_inputs, [1, 0, 2])

      if hps.init_dec_state == 'fwbw':
        initial_dec_state = tf.layers.dense(tf.concat([fw_state, bw_state], -1), hps.num_hidden)
        initial_dec_state = tf.contrib.rnn.LSTMStateTuple(initial_dec_state[0], initial_dec_state[1])
      else:
        initial_dec_state = fw_state

      #projection_layer = tf.layers.Dense(vsize, use_bias=True)
      layer1 = tf.layers.Dense(1024, use_bias=True) #, activation=tf.nn.relu)
      layer2 = tf.layers.Dense(vsize, use_bias=True)
      projection_layer = StackedLayer([layer1, layer2])

      layer1_proj_length = tf.layers.Dense(2, use_bias=True)
      layer1_proj_coverage = tf.layers.Dense(hps.enc_timesteps, use_bias=True)

      with tf.variable_scope('decoder'):
        layer1_inputs = tf.ones([hps.FLAGS.model_max_sentence_count, hps.batch_size, 1])
        layer1_decoder_cell = tf.contrib.rnn.LSTMCell(hps.num_hidden, initializer=uniform_initializer)
        layer1_helper = tf.contrib.seq2seq.TrainingHelper(layer1_inputs, self.ss_count, time_major=True)
        layer1_decoder = tf.contrib.seq2seq.BasicDecoder(
          cell=layer1_decoder_cell,
          helper=layer1_helper,
          initial_state = initial_dec_state)
        layer1_outputs, layer1_states, layer1_output_lengths = tf.contrib.seq2seq.dynamic_decode(layer1_decoder, output_time_major=True)
        layer1_outputs = layer1_outputs.rnn_output
        layer1_max_length = tf.reduce_max(layer1_output_lengths)
        layer1_outputs = tf.pad(layer1_outputs, [[0, hps.FLAGS.model_max_sentence_count - layer1_max_length], [0, 0], [0, 0]])
        #layer1_outputs.rnn_output time_size x batch_size x output_dimension
        #layer1_outputs = tf.Print(layer1_outputs, [tf.shape(layer1_outputs), self.ss_count, layer1_outputs], summarize=300)
        layer1_outputs.set_shape([hps.FLAGS.model_max_sentence_count, hps.batch_size, hps.num_hidden])

        layer1_last_sentence = layer1_proj_length(layer1_outputs)
        layer1_targets = tf.one_hot(self.ss_count - 1, hps.FLAGS.model_max_sentence_count, dtype=tf.int32)
        layer1_mask = tf.sequence_mask(self.ss_count, hps.FLAGS.model_max_sentence_count, dtype=tf.float32)
        #layer1_targets = tf.Print(layer1_targets, [layer1_targets, self.ss_count, layer1_mask], summarize=300)
        self._loss_1 = tf.contrib.seq2seq.sequence_loss(layer1_last_sentence, layer1_targets, layer1_mask)

        if hps.mode != 'decode':
          cell_decoder = tf.contrib.rnn.LSTMCell(hps.num_hidden, initializer=uniform_initializer)
          attention = tf.contrib.seq2seq.LuongAttention(hps.num_hidden, emb_memory, memory_sequence_length=article_lens)
          cell_decoder = tf.contrib.seq2seq.AttentionWrapper(cell_decoder, attention, attention_layer_size=hps.num_hidden)
          layer2_outputs, layer2_targets, layer2_weights = [], [], []
          for i in range(hps.FLAGS.model_max_sentence_count):
            _inputs = tf.transpose(self.ss_ids[:, i, :-1])
            _inputs = tf.gather(embedding, _inputs)
            _targets = tf.transpose(self.ss_ids[:, i, 1:])
            _lengths = self.ss_lengths[:, i]
            _masks = tf.transpose(tf.sequence_mask(_lengths, hps.FLAGS.model_max_sentence_length, dtype=tf.float32))

            helper = tf.contrib.seq2seq.TrainingHelper(_inputs, _lengths, time_major=True)
            _cell_state = tf.contrib.rnn.LSTMStateTuple(layer1_outputs[i], tf.zeros_like(initial_dec_state[1]))
            _cell_state = cell_decoder.zero_state(hps.batch_size, tf.float32).clone(cell_state=_cell_state)
            decoder = tf.contrib.seq2seq.BasicDecoder(
              cell = cell_decoder,
              helper = helper,
              initial_state = _cell_state,
              output_layer=projection_layer)
            outputs, _, output_lengths = tf.contrib.seq2seq.dynamic_decode(decoder, output_time_major=True)
            outputs = outputs.rnn_output
            max_len = tf.reduce_max(output_lengths)
            layer2_outputs.append(outputs)
            layer2_targets.append(_targets[:max_len, :])
            layer2_weights.append(_masks[:max_len, :])
          outputs = tf.concat(layer2_outputs, 0)
          targets = tf.concat(layer2_targets, 0)
          loss_weights = tf.concat(layer2_weights, 0)
          self._loss = tf.contrib.seq2seq.sequence_loss(outputs, targets, loss_weights)
          self._loss += self._loss_1
          tf.summary.scalar('loss', self._loss)
        else:
          predicted_ids = []
          emb_memory = tf.contrib.seq2seq.tile_batch(emb_memory, multiplier=hps.beam_size)
          article_lens = tf.contrib.seq2seq.tile_batch(article_lens, multiplier=hps.beam_size)
          cell_decoder = tf.contrib.rnn.LSTMCell(hps.num_hidden, initializer=uniform_initializer)
          attention = tf.contrib.seq2seq.LuongAttention(hps.num_hidden, emb_memory, memory_sequence_length=article_lens)
          cell_decoder = tf.contrib.seq2seq.AttentionWrapper(cell_decoder, attention, attention_layer_size=hps.num_hidden)
          start_token_ids = tf.fill([hps.batch_size], self._vocab.token_start_id)
          end_token_id = self._vocab.token_end_id
          for i in range(hps.FLAGS.model_max_sentence_count):
            _cell_state = tf.contrib.rnn.LSTMStateTuple(layer1_outputs[i], tf.zeros_like(initial_dec_state[1]))
            _cell_state = tf.contrib.seq2seq.tile_batch(_cell_state, multiplier=hps.beam_size)
            _cell_state = cell_decoder.zero_state(hps.batch_size * hps.beam_size, tf.float32).clone(cell_state=_cell_state)

            my_decoder = tf.contrib.seq2seq.BeamSearchDecoder(
              cell=cell_decoder,
              embedding=embedding,
              start_tokens=start_token_ids, end_token=end_token_id,
              initial_state=_cell_state,
              beam_width=hps.beam_size,
              output_layer=projection_layer)
            outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(my_decoder, maximum_iterations=hps.FLAGS.model_max_sentence_length, output_time_major=True)
            predicted_ids.append(tf.transpose(outputs.predicted_ids[:, :, 0]))
            predicted_ids.append([[self._vocab.token_eos_id]] * hps.batch_size)
          self._predicted_ids = tf.concat(predicted_ids, -1)

  def _add_train_op(self):
    self._train_op = tf.train.AdamOptimizer(epsilon=self._hps.adam_epsilon).minimize(self._loss, global_step=self.global_step, name='train_op')

  def build_graph(self):
    self._setup_model_input()
    self._add_seq2seq()
    self.global_step = tf.train.get_or_create_global_step()
    if self._hps.mode == 'train':
      self._add_train_op()
    self._summaries = tf.summary.merge_all()

