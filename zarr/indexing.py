# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division
import numbers
import itertools
import collections


import numpy as np


def is_integer(x):
    return isinstance(x, numbers.Integral)


def is_integer_array(x):
    return hasattr(x, 'dtype') and x.dtype.kind in 'ui'


def is_bool_array(x):
    return hasattr(x, 'dtype') and x.dtype == bool


def normalize_integer_selection(dim_sel, dim_len):

    # normalize type to int
    dim_sel = int(dim_sel)

    # handle wraparound
    if dim_sel < 0:
        dim_sel = dim_len + dim_sel

    # handle out of bounds
    if dim_sel >= dim_len or dim_sel < 0:
        raise IndexError('index out of bounds')

    return dim_sel


ChunkDimProjection = collections.namedtuple('ChunkDimProjection',
                                            ('dim_chunk_ix', 'dim_chunk_sel', 'dim_out_sel'))
"""A mapping from chunk to output array for a single dimension.

Parameters
----------
dim_chunk_ix
    Index of chunk.
dim_chunk_sel
    Selection of items from chunk array.
dim_out_sel
    Selection of items in target (output) array.

"""


class IntDimIndexer(object):

    def __init__(self, dim_sel, dim_len, dim_chunk_len):

        # check type
        if not is_integer(dim_sel):
            raise ValueError('selection must be an integer')

        # normalize
        dim_sel = normalize_integer_selection(dim_sel, dim_len)

        # store attributes
        self.dim_sel = dim_sel
        self.dim_len = dim_len
        self.dim_chunk_len = dim_chunk_len
        self.nitems = 1

    def __iter__(self):
        dim_chunk_ix = self.dim_sel // self.dim_chunk_len
        dim_offset = dim_chunk_ix * self.dim_chunk_len
        dim_chunk_sel = self.dim_sel - dim_offset
        dim_out_sel = None
        yield ChunkDimProjection(dim_chunk_ix, dim_chunk_sel, dim_out_sel)


def normalize_slice_selection(dim_sel, dim_len):

    # handle slice with None bound
    start = 0 if dim_sel.start is None else dim_sel.start
    stop = dim_len if dim_sel.stop is None else dim_sel.stop
    step = 1 if dim_sel.step is None else dim_sel.step

    # handle wraparound
    if start < 0:
        start = dim_len + start
    if stop < 0:
        stop = dim_len + stop

    # handle out of bounds
    if start < 0:
        raise IndexError('start index out of bounds: %s' % dim_sel.start)
    if stop < 0:
        raise IndexError('stop index out of bounds: %s' % dim_sel.stop)
    if start >= dim_len and dim_len > 0:
        raise IndexError('start index out of bounds: %ss' % dim_sel.start)
    if stop > dim_len:
        stop = dim_len
    if stop < start:
        stop = start

    return slice(start, stop, step)


class SliceDimIndexer(object):

    def __init__(self, dim_sel, dim_len, dim_chunk_len):

        # check type
        if not isinstance(dim_sel, slice):
            raise ValueError('selection must be a slice')

        # normalize
        dim_sel = normalize_slice_selection(dim_sel, dim_len)

        # store attributes
        self.dim_sel = dim_sel
        self.dim_len = dim_len
        self.dim_chunk_len = dim_chunk_len
        self.nitems = dim_sel.stop - dim_sel.start

    def __iter__(self):

        dim_chunk_from = self.dim_sel.start // self.dim_chunk_len
        dim_chunk_to = int(np.ceil(self.dim_sel.stop / self.dim_chunk_len))

        for dim_chunk_ix in range(dim_chunk_from, dim_chunk_to):

            dim_offset = dim_chunk_ix * self.dim_chunk_len

            if self.dim_sel.start <= dim_offset:
                # selection starts before current chunk
                dim_chunk_sel_start = 0
                dim_out_offset = dim_offset - self.dim_sel.start

            else:
                # selection starts within current chunk
                dim_chunk_sel_start = self.dim_sel.start - dim_offset
                dim_out_offset = 0

            if self.dim_sel.stop > (dim_offset + self.dim_chunk_len):
                # selection ends after current chunk
                dim_chunk_sel_stop = self.dim_chunk_len

            else:
                # selection ends within current chunk
                dim_chunk_sel_stop = self.dim_sel.stop - dim_offset

            dim_chunk_sel = slice(dim_chunk_sel_start, dim_chunk_sel_stop)
            dim_chunk_nitems = dim_chunk_sel_stop - dim_chunk_sel_start
            dim_out_sel = slice(dim_out_offset, dim_out_offset + dim_chunk_nitems)

            yield ChunkDimProjection(dim_chunk_ix, dim_chunk_sel, dim_out_sel)


def replace_ellipsis(selection, shape):

    selection = ensure_tuple(selection)

    # count number of ellipsis present
    n_ellipsis = sum(1 for i in selection if i is Ellipsis)

    if n_ellipsis > 1:
        # more than 1 is an error
        raise IndexError("an index can only have a single ellipsis ('...')")

    elif n_ellipsis == 1:
        # locate the ellipsis, count how many items to left and right
        n_items_l = selection.index(Ellipsis)  # items to left of ellipsis
        n_items_r = len(selection) - (n_items_l + 1)  # items to right of ellipsis
        n_items = len(selection) - 1  # all non-ellipsis items

        if n_items >= len(shape):
            # ellipsis does nothing, just remove it
            selection = tuple(i for i in selection if i != Ellipsis)

        else:
            # replace ellipsis with as many slices are needed for number of dims
            new_item = selection[:n_items_l] + ((slice(None),) * (len(shape) - n_items))
            if n_items_r:
                new_item += selection[-n_items_r:]
            selection = new_item

    # fill out selection if not completely specified
    if len(selection) < len(shape):
        selection += (slice(None),) * (len(shape) - len(selection))

    return selection


def ensure_tuple(v):
    if not isinstance(v, tuple):
        v = (v,)
    return v


ChunkProjection = collections.namedtuple('ChunkProjection',
                                         ('chunk_coords', 'chunk_selection', 'out_selection'))
"""A mapping of items from chunk to output array. Can be used to extract items from the chunk 
array for loading into an output array. Can also be used to extract items from a value array for 
setting/updating in a chunk array.

Parameters
----------
chunk_coords
    Indices of chunk.
chunk_selection
    Selection of items from chunk array.
out_selection
    Selection of items in target (output) array.

"""


def check_selection_length(selection, shape):
    if len(selection) > len(shape):
        raise IndexError('too many indices for array')
    if len(selection) < len(shape):
        raise IndexError('not enough indices for array')


# noinspection PyProtectedMember
class BasicIndexer(object):

    def __init__(self, selection, array):

        # ensure tuple
        selection = ensure_tuple(selection)

        # handle ellipsis
        selection = replace_ellipsis(selection, array._shape)
        check_selection_length(selection, array._shape)

        # setup per-dimension indexers
        dim_indexers = []
        for dim_sel, dim_len, dim_chunk_len in zip(selection, array._shape, array._chunks):

            if isinstance(dim_sel, int):
                dim_indexer = IntDimIndexer(dim_sel, dim_len, dim_chunk_len)

            elif isinstance(dim_sel, slice):
                dim_indexer = SliceDimIndexer(dim_sel, dim_len, dim_chunk_len)

            else:
                raise IndexError('bad selection type')

            dim_indexers.append(dim_indexer)

        self.dim_indexers = dim_indexers
        self.shape = tuple(s.nitems for s in self.dim_indexers
                           if not isinstance(s, IntDimIndexer))
        self.drop_axes = None

    def __iter__(self):
        for dim_projections in itertools.product(*self.dim_indexers):

            chunk_coords = tuple(p.dim_chunk_ix for p in dim_projections)
            chunk_selection = tuple(p.dim_chunk_sel for p in dim_projections)
            out_selection = tuple(p.dim_out_sel for p in dim_projections if p.dim_out_sel is not None)

            yield ChunkProjection(chunk_coords, chunk_selection, out_selection)


class BoolArrayDimIndexer(object):

    def __init__(self, dim_sel, dim_len, dim_chunk_len):

        # check number of dimensions
        if len(dim_sel.shape) > 1:
            raise IndexError('selection must be a 1d array')

        # check shape
        if dim_sel.shape[0] != dim_len:
            raise IndexError('selection has the wrong length')

        # store attributes
        self.dim_sel = dim_sel
        self.dim_len = dim_len
        self.dim_chunk_len = dim_chunk_len
        self.nchunks = int(np.ceil(self.dim_len / self.dim_chunk_len))

        # precompute number of selected items for each chunk
        self.chunk_nitems = np.zeros(self.nchunks, dtype='i8')
        for dim_chunk_ix in range(self.nchunks):
            dim_offset = dim_chunk_ix * self.dim_chunk_len
            self.chunk_nitems[dim_chunk_ix] = np.count_nonzero(
                self.dim_sel[dim_offset:dim_offset + self.dim_chunk_len]
            )
        self.chunk_nitems_cumsum = np.cumsum(self.chunk_nitems)
        self.nitems = self.chunk_nitems_cumsum[-1]
        self.dim_chunk_ixs = np.nonzero(self.chunk_nitems)[0]

    def __iter__(self):

        # iterate over chunks with at least one item
        for dim_chunk_ix in self.dim_chunk_ixs:

            # find region in chunk
            dim_offset = dim_chunk_ix * self.dim_chunk_len
            dim_chunk_sel = self.dim_sel[dim_offset:dim_offset + self.dim_chunk_len]

            # pad out if final chunk
            if dim_chunk_sel.shape[0] < self.dim_chunk_len:
                tmp = np.zeros(self.dim_chunk_len, dtype=bool)
                tmp[:dim_chunk_sel.shape[0]] = dim_chunk_sel
                dim_chunk_sel = tmp

            # find region in output
            if dim_chunk_ix == 0:
                start = 0
            else:
                start = self.chunk_nitems_cumsum[dim_chunk_ix - 1]
            stop = self.chunk_nitems_cumsum[dim_chunk_ix]
            dim_out_sel = slice(start, stop)

            yield ChunkDimProjection(dim_chunk_ix, dim_chunk_sel, dim_out_sel)


class IntArrayDimIndexer(object):
    """Integer array selection against a single dimension."""

    def __init__(self, dim_sel, dim_len, dim_chunk_len):

        # ensure array
        dim_sel = np.asanyarray(dim_sel)

        # check number of dimensions
        if dim_sel.ndim != 1:
            raise IndexError('selection must be a 1d array')

        # check dtype
        if dim_sel.dtype.kind not in 'ui':
            raise IndexError('selection must be an integer array')

        # handle wraparound
        loc_neg = dim_sel < 0
        if np.any(loc_neg):
            dim_sel[loc_neg] = dim_sel[loc_neg] + dim_len

        # handle out of bounds
        if np.any(dim_sel < 0) or np.any(dim_sel >= dim_len):
            raise IndexError('selection contains index out of bounds')

        # handle non-monotonic indices
        dim_sel_chunk = dim_sel // dim_chunk_len
        if np.any(np.diff(dim_sel) < 0):
            self.is_monotonic = False
            # sort indices to group by chunk
            self.dim_sort = np.argsort(dim_sel_chunk)
            self.dim_sel = np.take(dim_sel, self.dim_sort)

        else:
            self.is_monotonic = True
            self.dim_sort = None
            self.dim_sel = dim_sel

        # store attributes
        self.dim_len = dim_len
        self.dim_chunk_len = dim_chunk_len
        self.nchunks = int(np.ceil(self.dim_len / self.dim_chunk_len))
        self.nitems = len(self.dim_sel)

        # precompute number of selected items for each chunk
        # note: for dense integer selections, the division operation here is the bottleneck
        self.chunk_nitems = np.bincount(dim_sel_chunk, minlength=self.nchunks)
        self.chunk_nitems_cumsum = np.cumsum(self.chunk_nitems)
        self.dim_chunk_ixs = np.nonzero(self.chunk_nitems)[0]

    def __iter__(self):

        for dim_chunk_ix in self.dim_chunk_ixs:

            # find region in output
            if dim_chunk_ix == 0:
                start = 0
            else:
                start = self.chunk_nitems_cumsum[dim_chunk_ix - 1]
            stop = self.chunk_nitems_cumsum[dim_chunk_ix]
            if self.is_monotonic:
                dim_out_sel = slice(start, stop)
            else:
                dim_out_sel = self.dim_sort[start:stop]

            # find region in chunk
            dim_offset = dim_chunk_ix * self.dim_chunk_len
            dim_chunk_sel = self.dim_sel[start:stop] - dim_offset

            yield ChunkDimProjection(dim_chunk_ix, dim_chunk_sel, dim_out_sel)


def slice_to_range(s):
    return range(s.start, s.stop, 1 if s.step is None else s.step)


def ix_(*selection):
    """Convert an orthogonal selection to a numpy advanced (fancy) selection, with support for
    slices and single ints."""

    # replace slice and int as these are not supported by numpy ix_()
    selection = [slice_to_range(dim_sel) if isinstance(dim_sel, slice)
                 else [dim_sel] if isinstance(dim_sel, int)
                 else dim_sel
                 for dim_sel in selection]

    selection = np.ix_(*selection)

    return selection


def oindex(a, selection):
    """Implementation of orthogonal indexing with slices and ints."""
    drop_axes = tuple([i for i, s in enumerate(selection) if isinstance(s, int)])
    selection = ix_(*selection)
    result = a[selection]
    if drop_axes:
        result = result.squeeze(axis=drop_axes)
    return result


class OrthogonalIndexer(object):

    def __init__(self, selection, array):

        # ensure tuple
        selection = ensure_tuple(selection)

        # handle ellipsis
        selection = replace_ellipsis(selection, array._shape)

        # normalize list to array
        selection = replace_lists(selection)

        # validation - check dimensionality
        if len(selection) > len(array._shape):
            raise IndexError('too many indices for array')
        if len(selection) < len(array._shape):
            raise IndexError('not enough indices for array')

        # setup per-dimension indexers
        dim_indexers = []
        for dim_sel, dim_len, dim_chunk_len in zip(selection, array._shape, array._chunks):

            if is_integer(dim_sel):

                dim_indexer = IntDimIndexer(dim_sel, dim_len, dim_chunk_len)

            elif isinstance(dim_sel, slice):

                # normalize so we can check for step
                dim_sel = normalize_slice_selection(dim_sel, dim_len)

                # handle slice with step
                if dim_sel.step != 1:
                    dim_sel = np.arange(dim_sel.start, dim_sel.stop, dim_sel.step)
                    dim_indexer = IntArrayDimIndexer(dim_sel, dim_len, dim_chunk_len)
                else:
                    dim_indexer = SliceDimIndexer(dim_sel, dim_len, dim_chunk_len)

            elif is_integer_array(dim_sel):

                dim_indexer = IntArrayDimIndexer(dim_sel, dim_len, dim_chunk_len)

            elif is_bool_array(dim_sel):

                dim_indexer = BoolArrayDimIndexer(dim_sel, dim_len, dim_chunk_len)

            else:
                raise IndexError('bad selection type')

            dim_indexers.append(dim_indexer)

        self.dim_indexers = dim_indexers
        self.shape = tuple(s.nitems for s in self.dim_indexers
                           if not isinstance(s, IntDimIndexer))
        self.is_advanced = any([not isinstance(dim_indexer, (IntDimIndexer, SliceDimIndexer))
                                for dim_indexer in self.dim_indexers])
        if self.is_advanced:
            self.drop_axes = tuple([i for i, dim_indexer in enumerate(self.dim_indexers)
                                       if isinstance(dim_indexer, IntDimIndexer)])
        else:
            self.drop_axes = None

    def __iter__(self):
        for dim_projections in itertools.product(*self.dim_indexers):

            chunk_coords = tuple(p.dim_chunk_ix for p in dim_projections)
            chunk_selection = tuple(p.dim_chunk_sel for p in dim_projections)
            out_selection = tuple(p.dim_out_sel for p in dim_projections if p.dim_out_sel is not None)

            # handle advanced indexing arrays orthogonally
            if self.is_advanced:
                # numpy doesn't support orthogonal indexing directly as yet, so need to work
                # around via np.ix_. Also np.ix_ does not support a mixture of arrays and slices
                # or integers, so need to convert slices and integers into ranges.
                chunk_selection = ix_(*chunk_selection)

                # special case for non-monotonic indices
                if any([not isinstance(s, (int, slice)) for s in out_selection]):
                    out_selection = ix_(*out_selection)

            yield ChunkProjection(chunk_coords, chunk_selection, out_selection)


class OIndex(object):

    def __init__(self, array):
        self.array = array

    def __getitem__(self, selection):
        return self.array.get_orthogonal_selection(selection)

    def __setitem__(self, selection, value):
        return self.array.set_orthogonal_selection(selection, value)


def is_coordinate_selection(selection, array):
    return (
        (len(selection) == len(array._shape)) and
        all(
            [is_integer(dim_sel) or is_integer_array(dim_sel)
             for dim_sel in selection]
        )
    )


def is_mask_selection(selection, array):
    return (
        len(selection) == 1 and
        is_bool_array(selection[0]) and
        selection[0].shape == array.shape
    )


def replace_lists(selection):
    return tuple(
        np.asarray(dim_sel) if isinstance(dim_sel, list) else dim_sel
        for dim_sel in selection
    )


# noinspection PyProtectedMember
class CoordinateIndexer(object):

    def __init__(self, selection, array):

        # some initial normalization
        selection = ensure_tuple(selection)
        selection = tuple([i] if is_integer(i) else i for i in selection)
        selection = replace_lists(selection)

        # validation
        if not is_coordinate_selection(selection, array):
            # TODO refactor error messages for consistency
            raise IndexError('invalid coordinate selection')

        # attempt to broadcast selection - this will raise error if array dimensions don't match
        selection = np.broadcast_arrays(*selection)

        # normalization
        for dim_sel, dim_len in zip(selection, array.shape):

            # check number of dimensions, only support indexing with 1d array
            if len(dim_sel.shape) > 1:
                raise IndexError('selection must be 1-dimensional integer array')

            # handle wraparound
            loc_neg = dim_sel < 0
            if np.any(loc_neg):
                # TODO need to take a copy here, or OK to replace?
                dim_sel[loc_neg] = dim_sel[loc_neg] + dim_len

            # handle out of bounds
            if np.any(dim_sel < 0) or np.any(dim_sel >= dim_len):
                raise IndexError('index out of bounds')

        # compute flattened chunk index for each point in the selection
        chunks_multi_index = tuple(
            dim_sel // dim_chunk_len
            for (dim_sel, dim_chunk_len) in zip(selection, array._chunks)
        )
        chunks_raveled_indices = np.ravel_multi_index(chunks_multi_index,
                                                      dims=array._cdata_shape)

        # group points by chunk
        sel_sort = np.argsort(chunks_raveled_indices)
        chunks_raveled_indices = chunks_raveled_indices[sel_sort]
        selection = tuple(dim_sel[sel_sort] for dim_sel in selection)

        # store atrributes
        self.selection = selection
        self.sel_sort = sel_sort
        self.shape = selection[0].shape if selection[0].shape else (1,)
        self.drop_axes = None
        self.array = array

        # precompute number of selected items for each chunk
        self.chunk_nitems = np.bincount(chunks_raveled_indices, minlength=array.nchunks)
        self.chunk_nitems_cumsum = np.cumsum(self.chunk_nitems)
        self.chunk_rixs = np.nonzero(self.chunk_nitems)[0]

        # unravel
        self.chunk_mixs = np.unravel_index(self.chunk_rixs, dims=array._cdata_shape)

    def __iter__(self):

        # iterate over chunks
        for i, chunk_rix in enumerate(self.chunk_rixs):

            chunk_coords = tuple(m[i] for m in self.chunk_mixs)
            if chunk_rix == 0:
                start = 0
            else:
                start = self.chunk_nitems_cumsum[chunk_rix - 1]
            stop = self.chunk_nitems_cumsum[chunk_rix]
            out_selection = self.sel_sort[start:stop]

            chunk_offsets = tuple(
                dim_chunk_ix * dim_chunk_len
                for dim_chunk_ix, dim_chunk_len in zip(chunk_coords, self.array._chunks)
            )
            chunk_selection = tuple(
                dim_sel[start:stop] - dim_chunk_offset
                for (dim_sel, dim_chunk_offset) in zip(self.selection, chunk_offsets)
            )

            yield ChunkProjection(chunk_coords, chunk_selection, out_selection)


# noinspection PyProtectedMember
class MaskIndexer(CoordinateIndexer):

    def __init__(self, selection, array):

        # some initial normalization
        selection = ensure_tuple(selection)
        selection = replace_lists(selection)

        # validation
        if not is_mask_selection(selection, array):
            # TODO refactor error messages for consistency
            raise IndexError('invalid mask selection')

        # convert to indices
        selection = np.nonzero(selection[0])

        # delegate the rest to superclass
        super(MaskIndexer, self).__init__(selection, array)


class VIndex(object):

    def __init__(self, array):
        self.array = array

    def __getitem__(self, selection):
        selection = ensure_tuple(selection)
        selection = replace_lists(selection)
        if is_coordinate_selection(selection, self.array):
            return self.array.get_coordinate_selection(selection)
        elif is_mask_selection(selection, self.array):
            return self.array.get_mask_selection(selection)
        else:
            raise IndexError('unsupported selection')

    def __setitem__(self, selection, value):
        selection = ensure_tuple(selection)
        selection = replace_lists(selection)
        if is_coordinate_selection(selection, self.array):
            return self.array.set_coordinate_selection(selection, value)
        elif is_mask_selection(selection, self.array):
            return self.array.set_mask_selection(selection, value)
        else:
            raise IndexError('unsupported selection')