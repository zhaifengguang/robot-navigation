import tensorflow as tf
import numpy as np
import sys,os

from nav_model import NavModel
from utils import get_landmark_set, get_objects_set, get_batch_world_context, get_sparse_world_context, move
from custom_nn import CustomLSTMCell


class Baseline(NavModel):

	def __init__(self, config, is_training=True):
		# Maps' feature dictionary
		self._map_feature_dict = get_landmark_set(self._maps['grid']) # all featureas are the same for each map
		self._map_objects_dict = get_objects_set(self._maps['grid'])

		self._max_encoder_unrollings 	= config.encoder_unrollings
		self._max_decoder_unrollings 	= config.decoder_unrollings
		self._num_actions				= config.num_actions
		self._vocab_size 				= config.vocab_size
		self._y_size  					= 4*len(self._map_feature_dict) + len(self._map_objects_dict)

		# Model parameters
		self._n_hidden 					 	= config.num_nodes 	# same for encoder and decoder
		self._embedding_world_state_size = config.embedding_world_state_size

		self._init_learning_rate 			= tf.constant(config.learning_rate)		
		self._learning_rate 		 			= self._init_learning_rate
		self._learning_rate_decay_factor = config.learning_rate_decay_factor

		self._max_gradient_norm	= config.max_gradient_norm/3.0
		
		# debug parameters
		self._train_dir = "tmp/"			# dir where checkpoint files will be saves		
		# merging and writing vars
		self._writer = None
		# Dropout rate
		keep_prob = config.dropout_rate

		## TRAINING Placeholders
		self._encoder_inputs = []
		self._encoder_unrollings = tf.placeholder('int64')

		self._decoder_outputs = []
		self._decoder_unrollings = tf.placeholder('int64')
		self._world_state_vectors = [] 	# original sample structure, containing complete path, start_pos, end_pos, and map name
		for i in xrange(self._max_encoder_unrollings):
			self._encoder_inputs.append( tf.placeholder(tf.float32,shape=[1,self._vocab_size], name='x') )
		for i in xrange(self._max_decoder_unrollings):
			self._decoder_outputs.append( tf.placeholder(tf.int32,shape=[1], name='actions') )
			self._world_state_vectors.append( tf.placeholder(tf.float32,shape=[1,self._y_size], name='world_vect') )

		## TESTING / VALIDATION Placeholders
		self._test_st = tf.placeholder(tf.float32, [1, self._n_hidden], name='test_st')
		self._test_ct = tf.placeholder(tf.float32, [1, self._n_hidden], name='test_ct')
		self._test_yt = tf.placeholder(tf.float32, [1, self._y_size], name='test_yt')
		self._test_decoder_output = tf.placeholder(tf.float32,shape=[1,self._num_actions], name='test_action')


		with tf.name_scope('Weights') as scope:
			# Encoder - decoder transition
			w_trans_s = tf.Variable(weight_initializer((self._n_hidden, self._n_hidden)), name='w_trans_s')
			b_trans_s = tf.Variable(tf.zeros([1,self._n_hidden	]), name='b_trans_s')
			w_trans_c = tf.Variable(weight_initializer((self._n_hidden, self._n_hidden)), name='w_trans_c')
			b_trans_c = tf.Variable(tf.zeros([1,self._n_hidden	]), name='b_trans_c')
			# LSTM to softmax layer.
			wo = tf.Variable(weight_initializer((self._n_hidden , self._num_actions)), name='wo')
			b_o = tf.Variable(tf.zeros(			 [1 			    , self._num_actions]), name='bo')

		#######################################################################################################################
		
		with tf.variable_scope('Encoder') as scope:
			## Encoder
			enc_cell = CustomLSTMCell(self._n_hidden, forget_bias=1.0, input_size=self._vocab_size)
			enc_cell_dp = tf.nn.rnn_cell.DropoutWrapper(enc_cell,output_keep_prob=keep_prob)

			hs,last_state = tf.nn.rnn(	enc_cell_dp,
									self._encoder_inputs,
									dtype=tf.float32,
									sequence_length = self._encoder_unrollings*tf.ones([1],tf.int64),
									scope=scope 																			# cell scope: Encoder/BasicLSTMCell/...
									)
			c_last, h_last = tf.split(1, 2, last_state)
			#END-ENCODER-SCOPE

		#######################################################################################################################
			# transition
		with tf.variable_scope('Transition') as scope:
			st = tf.tanh( tf.matmul(h_last,w_trans_s)+b_trans_s , name='s_0')
			ct = tf.tanh( tf.matmul(c_last,w_trans_c)+b_trans_c , name='c_0')

		#######################################################################################################################			
		## Decoder loop
		with tf.variable_scope('Decoder') as scope:
			# Definition of the cell computation.
			dec_cell = CustomLSTMCell(self._n_hidden, forget_bias=1.0, input_size=self._y_size)
			dec_cell_dp = tf.nn.rnn_cell.DropoutWrapper(dec_cell,output_keep_prob=keep_prob)
			logits = []

			dec_outs,_ = tf.nn.rnn(dec_cell_dp,
									 inputs = self._world_state_vectors,
									 initial_state=tf.concat(1,[ct,st]),
									 dtype=tf.float32,
									 sequence_length = self._decoder_unrollings*tf.ones([1],tf.int64),
									 scope=scope 																			# cell scope: Decoder/BasicLSTMCell/...
									)
			self._train_predictions = []
			for out in dec_outs:
				logit = tf.matmul(out,wo)+b_o
				logits.append(logit)
				self._train_predictions.append( tf.nn.softmax(logit,name='prediction') )

			#for _ in range(len(dec_outs),self._max_decoder_unrollings):
			#	logits.append(tf.zeros([1,self._num_actions]))

			# Loss definition
			nopad_dec_outputs = [out for i,out in enumerate(self._decoder_outputs) if i<len(dec_outs)]
			self._loss = tf.nn.seq2seq.sequence_loss(logits,
																 targets=nopad_dec_outputs,
																 weights=[tf.ones([1],dtype=tf.float32)]*self._max_decoder_unrollings,
																 name='train_loss')
			#scope.reuse_variables()
			#END-DECODER-SCOPE
		###################################################################################################################
		# TESTING
		# Decode one step at a time, the world state vector is defined externally
		with tf.variable_scope('Encoder',reuse=True) as scope:
			test_hs,test_last_st = tf.nn.rnn( enc_cell,
														 self._encoder_inputs,
														 dtype=tf.float32,
														 sequence_length = self._encoder_unrollings*tf.ones([1],tf.int64),
														 scope=scope
														)

		test_c_last, test_h_last = tf.split(1, 2, test_last_st)

		self._test_s0 = tf.tanh( tf.matmul(test_h_last,w_trans_s)+b_trans_s , name='test_s0')
		self._test_c0 = tf.tanh( tf.matmul(test_c_last,w_trans_c)+b_trans_c , name='test_c0')
		
		with tf.variable_scope('Decoder',reuse=True) as scope:
			_,temp = dec_cell(self._test_yt,tf.concat(1, [self._test_ct, self._test_st]),scope="CustomLSTMCell")	# joins scopes -> Decoder/BasicLSTMCell
			self._next_ct,self._next_st = tf.split(1, 2, temp)

		# softmax layer,
		logit = tf.matmul(self._next_st,wo) + b_o
		self._test_prediction = tf.nn.softmax(logit,name='inf_prediction')
		# Loss definition
		self._test_loss = tf.nn.softmax_cross_entropy_with_logits(logit,self._test_decoder_output, name="test_loss")
			

		with tf.variable_scope('Optimization') as scope:
			# Optimizer setup
			self._global_step = tf.Variable(0)
			
			self._learning_rate = tf.train.exponential_decay(self._init_learning_rate,
															 self._global_step, 
															 10000,
															 self._learning_rate_decay_factor,
															 staircase=True)
			
			
			#params = tf.trainable_variables()
			#optimizer = tf.train.GradientDescentOptimizer(self._learning_rate)
			optimizer = tf.train.AdamOptimizer(learning_rate=self._learning_rate,
														  epsilon=1e-1)
			# Gradient clipping
			#gradients = tf.gradients(self._loss,params)
			gradients,params = zip(*optimizer.compute_gradients(self._loss))
			self._clipped_gradients, self._global_norm = tf.clip_by_global_norm(gradients, self._max_gradient_norm)
			# Apply clipped gradients
			self._optimizer = optimizer.apply_gradients( zip(self._clipped_gradients, params), global_step=self._global_step )

		### Summaries
		clipped_resh = [tf.reshape(tensor,[-1]) for tensor in self._clipped_gradients if tensor]
		clipped_resh = tf.concat(0,clipped_resh)
		# weight summaries
		_all = [tf.reshape(tf.to_float(tensor),[-1]) for tensor in tf.trainable_variables() if tensor]
		_all = tf.concat(0,_all)

		# sum strings
		_ = tf.scalar_summary("loss",self._loss)
		_ = tf.scalar_summary('global_norm',self._global_norm)
		_ = tf.scalar_summary('learning rate',self._learning_rate)
		_ = tf.histogram_summary('clipped_gradients', clipped_resh)
		_ = tf.histogram_summary('trainable vars', _all)

		# checkpoint saver
		#self.saver = tf.train.Saver(tf.all_variables())
		self._merged = tf.merge_all_summaries()
		
		self.kk=0

def weight_initializer(shape,dtype=tf.float32):
	u,_,v = np.linalg.svd(np.random.random(size=shape),full_matrices=False )
	weight_init = []
	if u.shape==shape:
		weight_init = u
	else:
		weight_init = v
	dtype_np = np.float32 if dtype==tf.float32 else np.float64
	return tf.constant(np.array(weight_init,dtype=dtype_np))