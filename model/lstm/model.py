# -*- coding:utf8 -*-
import tensorflow as tf
from tensorflow.contrib.rnn import LSTMCell
from tensorflow.contrib.layers import linear
from util.dictutil import load_dict
from util.datautil import batch_generator, load_corpus, batch_op, seq_index, dinput_op, loadPretrainedVector
from conf.profile import TOKEN_EOS, TOKEN_PAD, TOKEN_BOS, TOKEN_UNK
import numpy as np

# Configuration
class Config(object):
    def __init__(self):
        self.embedding_size = 50
        self.hidden_unit = 128
        self.save_path = "./save/lstm/"
        self.model_name = "LSTM-Model"
        self.dict_file = "./dict/dict_500.dict"
        self.corpus_file = "./data/split_valid.json"
        self.vector_file = "./dict/vector/wiki.zh.small.text.vector"
        self.vocab_size = 500
        self.max_batch = 1001
        self.save_step = 200
        self.batch_size = 5
        self.max_generate_len = 10

        self.is_beams = True
        self.beam_size = 3

        self.is_sample = True

        self.is_pretrained = True

class Model(object):

    def __init__(self, config):

        #self.vocab_to_idx = config.vocab_to_idx
        #self.idx_to_vocab = config.idx_to_vocab

        self.save_path = config.save_path
        self.model_name = config.model_name
        self.save_step = config.save_step
        self.max_batch = config.max_batch
        self.max_generate_len = config.max_generate_len
        self.batch_size = config.batch_size

        self.is_beams = config.is_beams
        if self.is_beams: self.beam_size = config.beam_size
        self.is_sample = config.is_sample

        self.corpus_file = config.corpus_file

        self.is_pretrained = config.is_pretrained

        self.idx_start = 0 # 0
        self.idx_eos = 1 # 1
        self.idx_pad = 2 # 2
        self.idx_unk = 3 # 3

        self.vocab_size = vocab_size = config.vocab_size
        self.embedding_size = embedding_size = config.embedding_size
        self.hidden_unit = hidden_unit = config.hidden_unit

        self.encoder_inputs = tf.placeholder(dtype=tf.int32, shape=(None, None), name='encoder_inputs')
        self.encoder_inputs_length = tf.placeholder(dtype=tf.int32, shape=(None,), name='encoder_inputs_length')
        self.decoder_inputs = tf.placeholder(dtype=tf.int32, shape=(None, None), name='decoder_inputs')
        self.decoder_targets = tf.placeholder(dtype=tf.int32, shape=(None, None), name='decoder_targets')

        with tf.device("/cpu:0"):
            if config.is_pretrained:
                self.vector_file = config.vector_file
                self.embeddings = tf.Variable(tf.constant(0.0, shape=[vocab_size, embedding_size]),
                                              trainable=False, name='embeddings')
                self.embeddings_placeholder = tf.placeholder(tf.float32, shape=[vocab_size, embedding_size]),
                self.embedding_init = self.embeddings.assign(tf.reshape(self.embeddings_placeholder, [vocab_size, embedding_size]))
            else:
                self.dict_file = config.dict_file
                self.embeddings = tf.Variable(tf.random_uniform([vocab_size, embedding_size], -1.0, 1.0), dtype=tf.float32)

        encoder_inputs_embedded = tf.nn.embedding_lookup(self.embeddings, self.encoder_inputs)

        encoder_cell = LSTMCell(num_units=hidden_unit)

        with tf.variable_scope("encoder"):
            [self.encoder_outputs,
             self.encoder_final_state] = tf.nn.dynamic_rnn(cell=encoder_cell,
                                                           inputs=encoder_inputs_embedded,
                                                           dtype=tf.float32,
                                                           time_major=True)

        decoder_cell = LSTMCell(num_units=hidden_unit)
        self.initial_state = self.encoder_final_state
        decoder_inputs_embedded = tf.nn.embedding_lookup(self.embeddings, self.decoder_inputs)
        with tf.variable_scope("decoder"):
            (self.decoder_outputs,
             self.decoder_final_state) = tf.nn.dynamic_rnn(decoder_cell,
                                                           decoder_inputs_embedded,
                                                           initial_state=self.initial_state,
                                                           dtype=tf.float32,
                                                           time_major=True)

        self.decoder_logits = linear(self.decoder_outputs, vocab_size)

        self.prob = tf.nn.softmax(self.decoder_logits)

        self.decoder_prediction = tf.argmax(self.decoder_logits, 2)

        stepwise_cross_entropy = tf.nn.softmax_cross_entropy_with_logits(
            labels=tf.one_hot(self.decoder_targets, depth=vocab_size, dtype=tf.float32),
            logits=self.decoder_logits,
        )

        self.loss = tf.reduce_mean(stepwise_cross_entropy)
        self.train_op = tf.train.AdamOptimizer().minimize(self.loss)

    def variables_init(self, sess):
        sess.run(tf.global_variables_initializer())
        self.dict_init(sess)
        self.batcher = batch_generator(load_corpus(self.corpus_file),
                                       batch_size=self.batch_size,
                                       word_to_index=self.vocab_to_idx)

    def dict_init(self, sess):
        if self.is_pretrained:
            self.vocab_to_idx, self.idx_to_vocab, vocab_emb = loadPretrainedVector(self.vocab_size, self.embedding_size, self.vector_file)
            sess.run(self.embedding_init, feed_dict={self.embeddings_placeholder: vocab_emb})
        else:
            self.vocab_to_idx, self.idx_to_vocab = load_dict(self.dict_file)

    def train(self, sess):
        loss_track = []
        for batch in range(self.max_batch):
            fd = self.next_feed()
            _, l = sess.run([self.train_op, self.loss], fd)
            loss_track.append(l)

            if batch % self.save_step == 0 and batch != 0:
                print('batch {}'.format(batch))
                print('  minibatch loss: {}'.format(sess.run(self.loss, fd)))
                predict_ = sess.run(self.decoder_prediction, fd)
                self.__print_result(fd[self.decoder_targets], predict_)
                self.save(sess, batch)

    def next_feed(self):
        [r_q, r_a, r_qe, r_ae] = next(self.batcher)
        encoder_inputs_, encoder_inputs_length_ = batch_op(r_q, self.idx_pad)
        decoder_targets_, _ = batch_op(r_a, self.idx_pad)
        decoder_inputs_, _ = dinput_op(r_a, self.idx_pad, self.idx_start)

        return {
            self.encoder_inputs: encoder_inputs_,
            self.encoder_inputs_length: encoder_inputs_length_,
            self.decoder_inputs:decoder_inputs_,
            self.decoder_targets: decoder_targets_
        }

    # 生成回复的方法.
    # 思路:   1.首先将Question传入Encoder,得到隐藏状态c,h;
    #        2.传入<start>生成第一个单词,并得到decoder的新隐藏状态c,h;
    #        3.传入刚才生成的单词,继续预测下一个单词.
    def generate(self, sess, inp):
        inp_index = [seq_index(self.vocab_to_idx, inp)]
        einp, einp_len = batch_op(inp_index, self.idx_pad)
        state_ = sess.run([self.encoder_final_state],
                          feed_dict = {self.encoder_inputs: einp,
                                       self.encoder_inputs_length: einp_len})

        if self.is_beams:
            beams = [(0.0, "", [])]
            tdata = self.vocab_to_idx[TOKEN_BOS]
            prob_, state_ = sess.run([self.prob, self.decoder_final_state],
                                     feed_dict={self.decoder_inputs: [[tdata]],
                                                self.initial_state: state_})

            y = np.log(1e-20 + prob_.reshape(-1))
            if self.is_sample:
                top_indices = np.random.choice(self.vocab_size, self.beam_size, replace=False, p=prob_.reshape(-1))
            else:
                top_indices = np.argsort(-y)
            b = beams[0]
            beam_candidates = []
            for bc in range(self.beam_size):
                vocab_idx = top_indices[bc]
                beam_candidates.append((b[0]+y[vocab_idx], b[1]+self.idx_to_vocab[vocab_idx], vocab_idx, state_))
            beam_candidates.sort(key=lambda x:x[0], reverse=True)
            beams = beam_candidates[:self.beam_size]
            for _ in range(self.max_generate_len-1):
                beam_candidates = []
                for b in beams:
                    tdata = np.int32(b[2])
                    prob_, state_ = sess.run([self.prob, self.decoder_final_state],
                                             feed_dict={self.decoder_inputs: [[tdata]],
                                                        self.initial_state: b[3]})
                    y = np.log(1e-20 + prob_.reshape(-1))
                    if self.is_sample:
                        top_indices = np.random.choice(self.vocab_size, self.beam_size, replace=False, p=prob_.reshape(-1))
                    else:
                        top_indices = np.argsort(-y)
                    for bc in range(self.beam_size):
                        vocab_idx = top_indices[bc]
                        beam_candidates.append((b[0]+y[vocab_idx], b[1]+self.idx_to_vocab[vocab_idx], vocab_idx, state_))
                    beam_candidates.sort(key=lambda x:x[0], reverse=True)
                    beams = beam_candidates[:self.beam_size]

            return beams[0][1]

        else:
            tdata = self.vocab_to_idx[TOKEN_BOS]
            prob_, state_ = sess.run([self.prob, self.decoder_final_state],
                                     feed_dict={self.decoder_inputs: [[tdata]],
                                                self.initial_state: state_})

            if self.is_sample:
                gen = np.random.choice(self.vocab_size, 1, replace=False, p=prob_.reshape(-1))
                gen = gen[0]
            else:
                gen = np.argmax(prob_.reshape(-1))

            tdata = np.int32(gen)
            response = self.idx_to_vocab[tdata]
            for _ in range(self.max_generate_len-1):
                prob_, state_ = sess.run([self.prob, self.decoder_final_state],
                                            feed_dict={self.decoder_inputs: [[tdata]],
                                                       self.initial_state: state_})
                if self.is_sample:
                    gen = np.random.choice(self.vocab_size, 1, replace=False, p=prob_.reshape(-1))
                    gen = gen[0]
                else:
                    gen = np.argmax(prob_.reshape(-1))
                tdata = np.int32(gen)
                response += self.idx_to_vocab[tdata]

            return response

    def __print_result(self, tar, pred):
        for i, (target, pred) in enumerate(zip(tar.T, pred.T)):
            print('  sample {}:'.format(i+1))
            str_tar = ""
            for j in target: str_tar += self.idx_to_vocab[j]
            print('    target     > ')
            print(str_tar)
            str_pred = ""
            for j in pred: str_pred += self.idx_to_vocab[j]
            print('    predicted     > ')
            print(str_pred)
            if i >= 2:
                break
            print(" ")

    def save(self, sess, step):
        saver = tf.train.Saver()
        saver.save(sess, self.save_path + self.model_name, global_step=step)

    def restore(self, sess, step):
        saver = tf.train.Saver()
        saver.restore(sess, self.save_path+self.model_name+'-'+str(step))
        return sess
