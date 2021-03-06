import sys
import os
import random
import collections
import numpy as np
import tensorflow as tf
from tensorflow.keras.layers import Input, Embedding, LSTM, Dense
from sklearn.metrics import accuracy_score

from vocab import Vocab
from sbs_data import load_data

def config_environment(model_dir):
	# to run multiple instances on the same GPU
	config = tf.ConfigProto()
	config.gpu_options.allow_growth=True
	tf.keras.backend.set_session(tf.Session(config=config))

	if not os.path.exists(model_dir):
		os.mkdir(model_dir)

def prepare_dataset(vocab, batch_size, data, swap_left_and_right=False):
	if len(data) % batch_size != 0:
		data = data[:-(len(data) % batch_size)]

	def words_to_ids(text, max_word_count):
		ids = [vocab.get_id_by_word(w) for w in text.split()[:max_word_count]]
		return [vocab.token_pad_id] * (max_word_count - len(ids)) + ids

	x0 = [words_to_ids(rec.query, 16) for rec in data]
	x1 = [words_to_ids(rec.snippet1, 100) for rec in data]
	x2 = [words_to_ids(rec.snippet2, 100) for rec in data]
	y = [rec.label+1 for rec in data]
	if swap_left_and_right:
		x0, x1, x2, y = x0 + x0, x1 + x2, x2 + x1, y + [2-val for val in y]
	return [x0, x1, x2], tf.keras.utils.to_categorical(y, 3)

def build_and_train_model(batch_size, model_dir, training_set):
	query_input = Input(shape=(16,), dtype='int32')
	snippet1_input = Input(shape=(100,), dtype='int32')
	snippet2_input = Input(shape=(100,), dtype='int32')

	embedding = Embedding(input_dim=50000, output_dim=64)
	query_embedding = embedding(query_input)
	snippet1_embedding = embedding(snippet1_input)
	snippet2_embedding = embedding(snippet2_input)

	query_lstm = LSTM(128)
	snippet_lstm = LSTM(128)
	query_encoding = query_lstm(query_embedding)
	snippet1_encoding = snippet_lstm(snippet1_embedding)
	snippet2_encoding = snippet_lstm(snippet2_embedding)

	all_encoding = tf.keras.layers.concatenate([query_encoding, snippet1_encoding, snippet2_encoding])
	X = Dense(256, activation='relu')(all_encoding)
	X = Dense(256, activation='relu')(X)
	X = Dense(256, activation='relu')(X)
	y = Dense(3, activation='softmax')(X)

	model = tf.keras.models.Model(inputs=[query_input, snippet1_input, snippet2_input], outputs=[y])
	#model.summary()
	model.compile(optimizer='adam', loss='categorical_crossentropy', metrics=['accuracy'])

	callbacks = [
		tf.keras.callbacks.ModelCheckpoint(model_dir + '/cp.best.model', save_best_only=True),
		tf.keras.callbacks.EarlyStopping(monitor='val_acc', patience=2),
		tf.keras.callbacks.TensorBoard(log_dir='{0}/tb'.format(model_dir)),
		]

	model.fit(*training_set, epochs=100, batch_size=batch_size, callbacks=callbacks, validation_split=0.1)
	model = tf.keras.models.load_model(model_dir + '/cp.best.model')
	return model

Config = collections.namedtuple('Config', 'vocab_path model_dir batch_size')
cfg = Config(
	vocab_path = 'trainingdata.vocab',
	model_dir = 'model',
	batch_size = 512,
	)

def train_then_predict(training_data, test_data):
	config_environment(cfg.model_dir)

	vocab = Vocab(cfg.vocab_path, 50000)
	training_set = prepare_dataset(vocab, cfg.batch_size*10, training_data)
	test_set = prepare_dataset(vocab, 1, test_data)

	model = build_and_train_model(cfg.batch_size, cfg.model_dir, training_set)
	pred = np.argmax(model.predict(test_set[0], batch_size=cfg.batch_size), -1)
	return [val-1 for val in pred]

if __name__ == '__main__':
	training_data, test_data = load_data() # struct data members: query snippet1 snippet2 weight label

	training_data2 = [rec for rec in training_data if rec.label != 0]
	test_data2 = [rec for rec in test_data if rec.label != 0]

	acc2, acc3 = [], []
	for i in range(10):
		pred = train_then_predict(training_data, test_data)
		acc3.append(accuracy_score([rec.label for rec in test_data], pred))

		pred = train_then_predict(training_data2, test_data2)
		acc2.append(accuracy_score([rec.label for rec in test_data2], pred))

		print('[{}] Binary classification accuracy on test_set : min={}, max={}, median={}'.format(i, np.min(acc2), np.max(acc2), np.median(acc2)))
		print('[{}] 3 categories classification accuracy on test_set : min={}, max={}, median={}'.format(i, np.min(acc3), np.max(acc3), np.median(acc3)))

