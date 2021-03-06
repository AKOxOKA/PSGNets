from __future__ import division, print_function, absolute_import

import os
import sys
import pdb

import numpy as np
import tensorflow as tf
import copy

# from graph.common import Graph, propdict

import psgnets.models.losses as losses
import psgnets.ops.utils as utils
from psgnets.ops.convolutional import conv, mlp
import psgnets.ops.rendering as rendering
import psgnets.ops.shape_coding as shape_coding
from psgnets.ops.dimensions import DimensionDict, OrderedDict
from .base import Model, Graph, propdict
from .preprocessing import preproc_rgb, preproc_hsv, delta_images

PRINT = False

DEFAULT_PRED_DIMS = OrderedDict([
    ('pred_depths', [1, lambda z: tf.minimum(z, -0.1)]),
    ('pred_images', [3, lambda im: tf.clip_by_value(preproc_hsv(im), -100., 100.)]),
    ('pred_normals', [3, lambda n: tf.nn.l2_normalize(n, axis=-1)])
])

class Decoder(Model):

    def __init__(
            self,
            name,
            model_func=None,
            input_signature=['inputs'],
            time_shared=True,
            **model_params
    ):
        self.name = name
        self.input_signature = input_signature
        super(Decoder, self).__init__(
            name=self.name, model_func=model_func, time_shared=time_shared, **model_params)

    def build_inputs(self, inputs, input_mapping):
        assert isinstance(inputs, (dict, OrderedDict))
        assert isinstance(input_mapping, dict)
        assert all((v in input_mapping.keys() for v in self.input_signature)), "Must pass one input per signature item in %s" % self.input_signature

        input_list = [inputs[input_mapping[nm]] for nm in self.input_signature]
        return input_list

    def rename_outputs(self, outputs):
        outputs = {
            self.name + '/' + k: outputs[k] for k in outputs.keys()}
        return outputs

    def build_model(self, func, trainable=True):
        assert isinstance(func, type(tf.identity)), func
        self.model_func_name = func.__name__
        def model(*args, **kwargs):
            call_params = kwargs
            call_params.update(self.params)
            return func(*args, **call_params)

        self.model_func = model

    def __call__(self, inputs, train=True,
                 input_mapping={'inputs': 'features/outputs'},
                 rename=True, **kwargs):

        kwargs['train'] = train
        decoder_inputs = self.build_inputs(inputs, input_mapping) # list
        decoder_inputs = self.reshape_batch_time(
            decoder_inputs, merge=True, **kwargs)

        print("decoder name", self.name)
        with tf.variable_scope(self.name):
            outputs = self.model_func(
                *decoder_inputs, **kwargs)

        if not isinstance(outputs, dict):
            assert isinstance(outputs, tf.Tensor)
            outputs = {'outputs': outputs}

        outputs = self.reshape_batch_time(outputs, merge=False, **kwargs)
        outputs = self.rename_outputs(outputs)
        self.outputs = outputs

        return outputs

class QtrDecoder(Decoder):

    def __init__(self, name, **model_params):
        super(QtrDecoder, self).__init__(
            name=name,
            model_func=spatial_attribute_decoder,
            input_signature=['nodes', 'segment_ids', 'dimension_dict'],
            time_shared=False,
            **model_params)

class FutureQtrDecoder(Decoder):

    def __init__(self, name, **model_params):
        super(FutureQtrDecoder, self).__init__(
            name=name,
            model_func=future_attribute_decoder,
            input_signature=['nodes', 'segment_ids', 'dimension_dict'],
            time_shared=False,
            **model_params)

class QsrDecoder(Decoder):

    def __init__(self, name, **model_params):
        super(QsrDecoder, self).__init__(
            name=name,
            model_func=shape_decoder,
            input_signature=['nodes', 'dimension_dict', 'size'],
            time_shared=False,
            **model_params)

class DeltaImages(Decoder):

    def __init__(self, name, **model_params):
        super(DeltaImages, self).__init__(
            name=name,
            model_func=delta_images,
            input_signature=['images'],
            time_shared=False,
            **model_params)

def spatial_attribute_decoder(
        nodes, segment_ids, dimension_dict=None,
        latent_vector_key='unary_attrs', key_pos=-1,
        hw_attr='hw_centroids',
        num_sample_points=4096, train=False,
        attribute_dims_to_decode=DEFAULT_PRED_DIMS,
        method='quadratic',
        **kwargs
):
    '''
    Inputs
    nodes: <propdict> of attr:tf.Tensor <tf.float32> pairs of shape [B,T,N,Dattr]
    segment_ids: [B,T,Hseg,Wseg] <tf.int32> of indices into the nodes dimension (N) of node attrs
    dimension_dict: <DimensionDict> of attr:[dstart,dend] pairs; modified in place to reflect new dim assignments/postprocs
    latent_vector_key: <str> which attribute of nodes to use to predict attrs
    hw_attr: <str> pattern to search for which attrs represent the (h,w) position of each node in an image
    attribute_dims_to_decode: <OrderedDict> of pred_attr:[dstart,dend] (or ndims <int>) that indicate which attrs will become predictions
    method=: <str> in ['constant', 'linear', 'quadratic'] that indicates how many coefficients to expand out rendered value as a function of (delta_h, delta_w) from segment centroid

    '''
    # get necessary node attributes
    assert isinstance(nodes, propdict), (nodes, type(nodes))
    Dims = dimension_dict or DimensionDict(nodes['vector'].shape.as_list()[1], {'hw_centroids': [-4,-2], 'valid': [-1,0]})
    hw_key = [k for k in Dims.sort().keys() if hw_attr in k][-1]
    node_hws = nodes.get(hw_key, Dims.get_tensor_from_attrs(nodes['vector'], hw_key))
    lat_key = [k for k in Dims.sort().keys() if latent_vector_key in k][key_pos]
    latent_vec = nodes.get(lat_key, Dims.get_tensor_from_attrs(nodes['vector'], lat_key))
    val_key = [k for k in Dims.sort().keys() if 'valid' in k][-1]
    valid_nodes = nodes.get(val_key, Dims.get_tensor_from_attrs(nodes['vector'], val_key))

    # sample spatial indices
    B,T,N,_ = valid_nodes.shape.as_list()
    _B,_T,H,W = segment_ids.shape.as_list()
    P = np.minimum(num_sample_points, H*W)
    assert [_B,_T] == [B,T] and (segment_ids.dtype == tf.int32), segment_ids
    spatial_inds = rendering.sample_image_inds( # [B,T,P,2] <tf.int32>
        out_shape=[B,T,P], im_size=[H,W], train=train)

    # figure out which nodes are being sampled and their offsets
    segment_ids, valid_segments, _ = utils.preproc_segment_ids(
        segment_ids, Nmax=N, return_valid_segments=True)
    segments_to_decode = rendering.get_image_values_from_indices(
        segment_ids[...,tf.newaxis], spatial_inds)# [B,T,P,1] <tf.int32>
    valid_segments_to_decode = rendering.get_image_values_from_indices(
        valid_segments[...,tf.newaxis], spatial_inds) # [B,T,P,1] <tf.float32>
    ones = tf.ones_like(segments_to_decode)
    segments_to_decode = tf.concat([ # [B,T,P,3] indices
        tf.reshape(tf.range(B, dtype=tf.int32), [B,1,1,1])*ones,
        tf.reshape(tf.range(T, dtype=tf.int32), [1,T,1,1])*ones,
        segments_to_decode], axis=-1)

    spatial_inds_float = tf.cast(spatial_inds, tf.float32)
    spatial_inds_float = -1.0 + tf.divide(
        spatial_inds_float,
        tf.reshape(tf.constant([(H-1.0)/2.0, (W-1.0)/2.0], tf.float32), [1,1,1,2])) # now in [-1.0, 1.0], same as node_hws
    centroids_to_decode = tf.gather_nd(node_hws, segments_to_decode)
    dH,dW = tf.split(spatial_inds_float - centroids_to_decode, [1,1], axis=-1) # [B,T,P,1] each

    # get latent vectors for each sampled position
    valid_vectors_to_decode = tf.gather_nd(valid_nodes, segments_to_decode) * valid_segments_to_decode
    latent_vectors_to_decode = tf.gather_nd(latent_vec, segments_to_decode) * valid_vectors_to_decode

    # assign dims to the latent vector and the dimension_dict
    n_coeffs_per_dim = {'constant':0, 'linear':2, 'quadratic':5}[method]
    D = latent_vectors_to_decode.shape.as_list()[-1]
    predDims = DimensionDict(D)
    dims_used=0
    for attr, dims in attribute_dims_to_decode.items():
        newdims = predDims.parse_dims(dims, start=dims_used, multiplier=(1+n_coeffs_per_dim), allow_expansion=False)
        predDims[attr] = newdims
        dims_used += newdims[1] - newdims[0]
    predDims[lat_key+'_remainder'] = [dims_used, predDims.ndims]
    Dims.insert_from(predDims, position=Dims[lat_key][0], expand=False)
    Dims.sort()

    # Do the texture "rendering" on each attribute
    spatial_pred_attrs = {}
    deltas = [1.0] + ([dH,dW] if method != 'constant' else []) + ([dH*dH, dH*dW, dW*dW] if method == 'quadratic' else [])
    for attr in attribute_dims_to_decode.keys():
        attr_vec = predDims.get_tensor_from_attrs(
            latent_vectors_to_decode, attr, postproc=False)
        attr_vec = tf.split(attr_vec, 1+n_coeffs_per_dim, axis=-1)
        func = predDims[attr][2]
        spatial_pred_attrs[attr] = func(tf.add_n([av * deltas[i] for i,av in enumerate(attr_vec)]))

    outputs = {
        'sampled_hw_inds': spatial_inds,
        'sampled_pred_attrs': spatial_pred_attrs,
        'sampled_valid_attrs': valid_vectors_to_decode
    }

    return outputs

def future_attribute_decoder(
        nodes, segment_ids, dimension_dict, train=False,
        flows_dims=('pred_flows', [[0,2]]), key_pos=0,
        back_flows_dims=None,
        depths_dims=('pred_depths', [[0,1]]),
        attribute_dims_to_decode={'pred_images': [3, preproc_hsv]},
        stop_gradient_attrs=True,
        **kwargs):

    assert isinstance(nodes, propdict), (nodes, type(nodes))
    Dims = dimension_dict
    nodes_valid = Dims.get_attr(nodes, 'valid', sort=True, with_key=False)
    nodes = nodes['vector']
    B,T,N,D = nodes.shape.as_list()
    assert Dims.ndims == D, (Dims, nodes)
    _,_,H,W = segment_ids.shape.as_list()

    ## produce the flows map

    nodes_flows = Dims.get_attr_dims(nodes, *flows_dims, position=key_pos, stop_gradient=True)
    if back_flows_dims is not None:
        nodes_back_flows = Dims.get_attr_dims(nodes, *back_flows_dims, position=-1, stop_gradient=True)
        assert nodes_back_flows.shape == nodes_flows.shape, (nodes_back_flows)
    else:
        nodes_back_flows = -1. * nodes_flows

    segment_ids = utils.preproc_segment_ids(segment_ids, N, False)
    fwd_flows = rendering.render_nodes_with_segment_ids(nodes_flows, segment_ids)
    bck_flows = rendering.render_nodes_with_segment_ids(nodes_back_flows, segment_ids)

    ## combine flows
    flows = tf.concat([
        -bck_flows[:,0:1],
        0.5 * (fwd_flows[:,1:-1] - bck_flows[:,1:-1]),
        fwd_flows[:,-1:]
    ], axis=1)
    flows = tf.stop_gradient(flows)

    ## propagate the index map
    _, contested_map, index_masks = rendering.propagate_index_map(
        segment_ids, flows, nodes_valid, **kwargs)

    ## resolve the depth ordering
    nodes_depths = Dims.get_attr_dims(nodes, *depths_dims, position=-1, stop_gradient=False)
    ddims = Dims[depths_dims[0]]
    Dims['pred_depths'] = [ddims[0] + depths_dims[1][0][0], ddims[0] + depths_dims[1][0][1]]
    assert nodes_depths.shape.as_list() == [B,T,N,1]
    depth_weights = rendering.resolve_depth_order(index_masks, nodes_depths, nodes_valid, **kwargs)

    ## render
    future_images = {
        'valid_pixels': tf.reduce_max(index_masks, axis=-1, keepdims=True),
        'contested_pixels': contested_map,
    }
    for attr, dims in attribute_dims_to_decode.items():
        ds = [[0,dims[0]]] if isinstance(dims[0], int) else dims[0]
        nodes_attr = Dims.get_attr_dims(nodes, attr, ds, position=-1)
        nodes_attr = tf.stop_gradient(nodes_attr) if stop_gradient_attrs else nodes_attr
        rend = rendering.render_attrs_from_segment_weights(
            depth_weights, nodes_attr, ds)
        rend = (dims[1] or tf.identity)(rend)
        future_images[attr] = rend

    return future_images

def shape_decoder(
        nodes,
        dimension_dict,
        size,
        num_constraints=8,
        train=False,
        shape_dims=('unary_attr', [[0,32]]),
        shape_key_pos=-1,
        shape_mlp_kwargs=None,
        shape_code_bias=None,
        depths_dims=('pred_flood', [[0,1]]),
        depths_key_pos=-1,
        depths_conv_kwargs=None,
        zero_max=False, valid_mask=True,
        hw_attr='hw_centroids',
        scale_codes_by_imsize=True,
        attribute_dims_to_decode={'pred_images': [3, preproc_hsv]},
        stop_gradient_attrs=True,
        stop_gradient_depths=False,
        **kwargs
):

    assert isinstance(nodes, propdict), (nodes, type(nodes))
    Dims = dimension_dict
    nodes_valid = Dims.get_attr(nodes, 'valid', sort=True, position=-1, with_key=False)
    nodes_hw = Dims.get_attr(nodes, hw_attr, sort=True, position=-1, with_key=False)
    nodes = nodes['vector']
    B,T,N,_ = nodes_valid.shape.as_list()
    H,W = size
    C = num_constraints

    ## for decoding into parabolae
    # shape_codes = Dims.get_attr_dims(nodes, *shape_dims, position=shape_key_pos, stop_gradient=False)
    shape_inputs = Dims.get_tensor_from_attr_dims(nodes, shape_dims, stop_gradient=False)
    if shape_mlp_kwargs is not None:
        mlp_kwargs = copy.deepcopy(shape_mlp_kwargs)
        mlp_kwargs['hidden_dims'] = mlp_kwargs.get('hidden_dims', []) + [4*C]
        shape_codes = mlp(inp=shape_inputs, scope='shape_code_mlp', **mlp_kwargs)
    else:
        shape_codes = shape_inputs
        skey = [k for k in Dims.sort().keys() if shape_dims[0] in k][shape_key_pos]
        sdims = Dims[skey]
        Dims['pred_shape_codes'] = [sdims[0] + shape_dims[1][0][0], sdims[0] + shape_dims[1][0][0] + 4*C]
        Dims[skey + '_qsr_remainder'] = [Dims['pred_shape_codes'][1], sdims[1]]

    assert shape_codes.shape.as_list()[-1] >= 4*C, (C, shape_codes, shape_dims)
    shape_codes = tf.reshape(shape_codes[...,:4*C], [B,T,N,C,4])

    ## scale the shape codes up to the image size
    if scale_codes_by_imsize:
        xy_scales = tf.constant([float(W-1)/2, float(H-1)/2, 1., np.sqrt(float(H-1)*float(W-1))/2], tf.float32)
        xy_scales = tf.reshape(xy_scales, [1,1,1,1,4])
    else:
        xy_scales = tf.ones([1,1,1,1,4], dtype=tf.float32)
    shape_codes *= xy_scales
    if shape_code_bias is not None:
        shape_codes += tf.reshape(
            tf.constant(shape_code_bias),
            [1,1,1,1,4])

    ## get centroids for each shape
    assert nodes_hw.shape.as_list()[-1] == 2, (nodes_hw, hw_attr)
    ch, cw = tf.unstack(nodes_hw, axis=-1)
    centroids_xy = tf.stack([cw, -ch], -1)
    translations = centroids_xy * xy_scales[...,0,0:2]

    ## TODO learnable scaling
    rotations = scales = None

    ## decode the shapes as images
    constraints, shapes = shape_coding.build_shape_from_codes(
        shape_codes, translations=translations, rotations=rotations, scales=scales, imsize=size, xy_input=True, **kwargs) # [B,T,N,H,W]
    shapes *= nodes_valid[...,tf.newaxis]
    if PRINT:
        shapes = tf.Print(shapes, [tf.reduce_max(shapes), tf.reduce_sum(nodes_valid, axis=[2,3])[0]], message='valid_shapes_max')

    ## for resolving depths order
    nodes_depths = Dims.get_attr_dims(nodes, *depths_dims, position=depths_key_pos, stop_gradient=stop_gradient_depths) # [B,T,N,D]
    if depths_conv_kwargs is not None:
        nodes_depths = nodes_depths[:,:,:,tf.newaxis,tf.newaxis] # [B,T,N,1,1,D]
        nodes_depths = tf.tile(nodes_depths, [1,1,1,H,W,1])
        hw_grid = tf.reshape(shape_coding.get_hw_grid(size), [1,1,1,H,W,2])
        delta_hws = hw_grid - nodes_hw[:,:,:,tf.newaxis,tf.newaxis,:]
        nodes_depths = tf.concat([nodes_depths, delta_hws, shapes[...,tf.newaxis]], axis=-1)
        nodes_depths = tf.reshape(nodes_depths, [B*T*N,H,W,-1])
        with tf.variable_scope("shapes_depth_ordering_conv"):
            nodes_depths = conv(nodes_depths, out_depth=1, **depths_conv_kwargs)
        nodes_depths = tf.reshape(nodes_depths, [B,T,N,H,W])
        nodes_depths = tf.transpose(nodes_depths, [0,1,3,4,2])

    ## resolve depth ordering
    shape_logits = rendering.resolve_depth_order(
        index_masks=tf.transpose(shapes, [0,1,3,4,2]), # [B,T,H,W,N]
        node_depths=nodes_depths,
        valid_nodes=nodes_valid,
        softmax=False, # logits
        valid_mask=valid_mask,
        **kwargs) # [B,T,H,W,N] softmaxed along last dimension
    ## prevent clipping and overflow
    if zero_max:
        shape_logits -= tf.reduce_max(shape_logits, axis=-1, keepdims=True)
    shape_probs = tf.nn.softmax(shape_logits * kwargs.get('beta', 1.), axis=-1)

    if PRINT:
        shape_logits = tf.Print(shape_logits, [tf.reduce_min(nodes_depths), tf.reduce_max(nodes_depths), tf.reduce_min(shapes), tf.reduce_max(shapes)], message='depths_and_shapes')
        shape_logits = tf.Print(shape_logits, [tf.reduce_max(shape_probs, axis=[2,3,4]), tf.argmax(shape_probs[:,:,32,32,:], axis=-1)], message='shape_probs')

    decoded = {
        'shapes': shape_probs if train else tf.argmax(shape_probs, axis=-1),
        'shape_logits': shape_logits if train else tf.argmax(shape_logits, axis=-1),
        'shape_constraints': constraints
    }
    shapes_valid = tf.transpose(tf.tile(nodes_valid[...,tf.newaxis], [1,1,1,H,W]), [0,1,3,4,2])
    decoded['shapes_valid'] = tf.reduce_max(shapes_valid, axis=-1)

    for attr, dims in attribute_dims_to_decode.items():
        ds = [[0,dims[0]]] if isinstance(dims[0], int) else dims[0]
        nodes_attr = Dims.get_attr_dims(nodes, attr, ds, position=-1)
        nodes_attr = tf.stop_gradient(nodes_attr) if stop_gradient_attrs else nodes_attr
        rend = rendering.render_attrs_from_segment_weights(
            shape_probs, nodes_attr, ds)
        rend = (dims[1] or tf.identity)(rend)
        decoded[attr] = rend

    return decoded
