# -*- coding: utf-8 -*-
# Copyright (c) Vispy Development Team. All Rights Reserved.
# Distributed under the (new) BSD License. See LICENSE.txt for more info.

from __future__ import division
import warnings

import numpy as np

from ..gloo import Texture2D, VertexBuffer
from ..color import get_colormap
from .shaders import Function, FunctionChain
from .transforms import NullTransform
from .visual import Visual
from ..io import load_spatial_filters


F64_PRECISION_WARNING = ("GPUs can't support floating point data with more "
                         "than 32-bits, precision will be lost due to "
                         "downcasting to 32-bit float.")


def _should_cast_to_f32(data_dtype):
    is_floating = np.issubdtype(data_dtype, np.floating)
    gt_float32 = data_dtype.itemsize > 4
    if is_floating and gt_float32:
        # OpenGL can't support floating point numbers greater than 32-bits
        warnings.warn(F64_PRECISION_WARNING)
        return True
    return False


class ScalableTexture2D(Texture2D):
    """Texture class for smarter scaling and internalformat decisions."""

    # dtype -> internalformat
    # 'r' will be replaced (if needed) with rgb or rgba depending on number of bands
    _texture_dtype_format = {
        np.float32: 'r32f',
        np.float64: 'r32f',
        np.uint8: 'r8',
        np.uint16: 'r16',
        np.uint32: 'r32',
        np.int8: 'r8',
        np.int16: 'r16',
        np.int32: 'r32',
    }

    def __init__(self, data=None, internalformat=None, allow_reformat=False,
                 **texture_kwargs):
        self._allow_reformat = allow_reformat
        self._auto_texture_format = False
        self._clim = None
        self._data_dtype = getattr(data, 'dtype', None)
        self._data_limits = None

        data = self._create_rep_array(data)
        internalformat = self._get_texture_format_for_data(data, internalformat)
        self._scale_texture_gpu = internalformat is not None
        super().__init__(data, internalformat=internalformat, **texture_kwargs)

    @property
    def clim(self):
        """Color limits of the texture's data."""
        return self._clim

    def set_clim(self, clim):
        """Set clim and return if a texture update is needed."""
        need_texture_upload = False
        if isinstance(clim, str):
            if clim != 'auto':
                raise ValueError('clim must be "auto" if a string')
            need_texture_upload = True
            self._clim = clim
        else:
            try:
                cmin, cmax = clim
            except (ValueError, TypeError):
                raise ValueError('clim must have two elements')
            # texture_limits will always be None for in-GPU scaling
            if self._data_limits is not None and (
                    (cmin < self._data_limits[0])
                    or (cmax > self._data_limits[1])
            ):
                need_texture_upload = True
            self._clim = (cmin, cmax)
        return need_texture_upload

    @property
    def clim_normalized(self):
        """Normalize current clims to match texture data inside the shader.

        If data is scaled on the CPU then the texture data will be in the range
        0-1 in the _build_texture() method. Inside the fragment shader the
        final contrast adjustment will be applied based on this normalized
        ``clim``.  If data is scaled only on the GPU then we only normalize
        the color limits when needed (for unsigned normalized integer
        internal formats). Otherwise, for internal formats that are not
        normalized such as floating point (ex. r32f) we can leave the ``clim``
        as is.

        """
        if self._scale_texture_gpu:
            # if the internalformat of the texture is normalized we need to
            # also normalize the clims so they match in-shader
            clim_min = self.normalize_value(self.clim[0], self._data_dtype)
            clim_max = self.normalize_value(self.clim[1], self._data_dtype)
            return clim_min, clim_max

        range_min, range_max = self._data_limits
        clim_min, clim_max = self.clim
        clim_min = (clim_min - range_min) / (range_max - range_min)
        clim_max = (clim_max - range_min) / (range_max - range_min)
        return clim_min, clim_max

    @property
    def is_normalized(self):
        """Whether the in-shader representation of this texture is normalized or not.

        Formats ending in 'f' (float), 'ui' (unsigned integral), or 'i'
        (integral) are not normalized in the GPU. Formats ending in "_snorm"
        are normalized on the range [-1, 1] based on the data type of the
        input data (ex. 0-255 for uint8). Formats with no data type suffix are
        normalized on the range [0, 1]. See
        https://www.khronos.org/opengl/wiki/Image_Format for more information.

        This property can be used to determine if input shader variables
        (uniforms, template variables) need to also be normalized. See
        :meth:`~BaseTexture.normalize_value` below.

        """
        if self.internalformat is None:
            return True
        return self.internalformat[-1] not in ('f', 'i')

    def normalize_value(self, val, input_data_dtype):
        """Normalize values to match in-shader representation of this shader.

        Parameters
        ----------
        val : int | float | ndarray
            Value(s) to normalize.
        input_data_dtype : numpy.dtype
            Data type of input data. The assumption is that the provided
            values to be normalized are in the same range as the input
            texture data and must be normalized in the same way.

        """
        if not self.is_normalized:
            return val
        dtype_info = np.iinfo(input_data_dtype)
        dmin = dtype_info.min
        dmax = dtype_info.max
        val = (val - dmin) / (dmax - dmin)
        # XXX: Do we need to handle _snorm differently?
        #  Not currently supported in vispy.
        return val

    @staticmethod
    def _data_num_channels(data):
        if data is not None:
            num_channels = data.shape[-1] if data.ndim == 3 else 1
        else:
            num_channels = 4
        return num_channels

    def _create_rep_array(self, data):
        """Get a representative array with an initial shape.

        Data will be filled in and the texture resized later.

        """
        num_channels = self._data_num_channels(data)
        return np.zeros((1, 1, num_channels))

    def _handle_auto_texture_format(self, texture_format, data):
        if isinstance(texture_format, str) and texture_format == 'auto':
            if data is None:
                warnings.warn("'texture_format' set to 'auto' but no data "
                              "provided. Falling back to CPU scaling.")
                texture_format = None
            else:
                texture_format = data.dtype.type
                self._auto_texture_format = True
        return texture_format

    def _get_gl_tex_format(self, texture_format, num_channels):
        if texture_format and not isinstance(texture_format, str):
            if texture_format not in self._texture_dtype_format:
                raise ValueError("Can't determine internal texture format for '{}'".format(texture_format))
            _should_cast_to_f32(texture_format)
            texture_format = self._texture_dtype_format[texture_format]
        # adjust internalformat for format of data (RGBA vs L)
        texture_format = texture_format.replace('r', 'rgba'[:num_channels])
        return texture_format

    def _get_texture_format_for_data(self, data, texture_format):
        if texture_format is not None:
            num_channels = self._data_num_channels(data)
            texture_format = self._handle_auto_texture_format(texture_format, data)
            texture_format = self._get_gl_tex_format(texture_format, num_channels)
        return texture_format

    @staticmethod
    def _scale_data_on_cpu(data, clim):
        if data.dtype == np.float64:
            data = data.astype(np.float32)
        data = data - clim[0]  # not inplace so we don't modify orig data
        if clim[1] - clim[0] > 0:
            data /= clim[1] - clim[0]
        else:
            data[:] = 1 if data[0, 0] != 0 else 0
        return data

    def set_data(self, data, offset=None, copy=False):
        """Upload new data to the GPU, scaling if necessary."""
        self._data_dtype = data.dtype

        clim = self._clim
        is_auto = isinstance(clim, str) and clim == 'auto'
        if data.ndim == 2 or data.shape[2] == 1:
            if is_auto:
                clim = np.min(data), np.max(data)
            clim = (np.float32(clim[0]), np.float32(clim[1]))
            if not self._scale_texture_gpu:
                data = self._scale_data_on_cpu(data, clim)
        elif is_auto:
            # assume that RGB data is already scaled (0, 1)
            clim = (0, 1)

        # XXX: Does this *always* need a colortransform update?
        self._clim = clim
        self._data_limits = None if self._scale_texture_gpu else clim
        ret = super().set_data(data, offset=offset, copy=copy)
        return ret


VERT_SHADER = """
uniform int method;  // 0=subdivide, 1=impostor
attribute vec2 a_position;
attribute vec2 a_texcoord;
varying vec2 v_texcoord;

void main() {
    v_texcoord = a_texcoord;
    gl_Position = $transform(vec4(a_position, 0., 1.));
}
"""

FRAG_SHADER = """
uniform vec2 image_size;
uniform int method;  // 0=subdivide, 1=impostor
uniform sampler2D u_texture;
varying vec2 v_texcoord;

vec4 map_local_to_tex(vec4 x) {
    // Cast ray from 3D viewport to surface of image
    // (if $transform does not affect z values, then this
    // can be optimized as simply $transform.map(x) )
    vec4 p1 = $transform(x);
    vec4 p2 = $transform(x + vec4(0, 0, 0.5, 0));
    p1 /= p1.w;
    p2 /= p2.w;
    vec4 d = p2 - p1;
    float f = p2.z / d.z;
    vec4 p3 = p2 - d * f;

    // finally map local to texture coords
    return vec4(p3.xy / image_size, 0, 1);
}


void main()
{
    vec2 texcoord;
    if( method == 0 ) {
        texcoord = v_texcoord;
    }
    else {
        // vertex shader outputs clip coordinates;
        // fragment shader maps to texture coordinates
        texcoord = map_local_to_tex(vec4(v_texcoord, 0, 1)).xy;
    }

    gl_FragColor = $color_transform($get_data(texcoord));
}
"""  # noqa

_interpolation_template = """
    #include "misc/spatial-filters.frag"
    vec4 texture_lookup_filtered(vec2 texcoord) {
        if(texcoord.x < 0.0 || texcoord.x > 1.0 ||
        texcoord.y < 0.0 || texcoord.y > 1.0) {
            discard;
        }
        return %s($texture, $shape, texcoord);
    }"""

_texture_lookup = """
    vec4 texture_lookup(vec2 texcoord) {
        if(texcoord.x < 0.0 || texcoord.x > 1.0 ||
        texcoord.y < 0.0 || texcoord.y > 1.0) {
            discard;
        }
        return texture2D($texture, texcoord);
    }"""

_apply_clim_float = """
    float apply_clim(float data) {
        if ($clim.x < $clim.y) {{
            data = clamp(data, $clim.x, $clim.y);
            data = clamp(data, $clim.x, $clim.y);
        }} else {{
            data = clamp(data, $clim.y, $clim.x);
            data = clamp(data, $clim.y, $clim.x);
        }}
        data = data - $clim.x;
        data = data / ($clim.y - $clim.x);
        return max(data, 0);
    }"""
_apply_clim = """
    vec4 apply_clim(vec4 color) {
        color.rgb = color.rgb - $clim.x;
        color.rgb = color.rgb / ($clim.y - $clim.x);
        return max(color, 0);
    }
"""

_apply_gamma_float = """
    float apply_gamma(float data) {
        return pow(data, $gamma);
    }"""
_apply_gamma = """
    vec4 apply_gamma(vec4 color) {
        color.rgb = pow(color.rgb, vec3($gamma));
        return color;
    }
"""

_null_color_transform = 'vec4 pass(vec4 color) { return color; }'
# FIXME: Is this bad for single band internalformats? ex. R8?
_c2l = 'float cmap(vec4 color) { return (color.r + color.g + color.b) / 3.; }'


def _build_color_transform(data, clim, gamma, cmap):
    # FIXME: This should probably be decided based on the texture internalformat
    if data.ndim == 2 or data.shape[2] == 1:
        # luminance data
        fclim = Function(_apply_clim_float)
        fgamma = Function(_apply_gamma_float)
        fun = FunctionChain(
            None, [Function(_c2l), fclim, fgamma, Function(cmap.glsl_map)]
        )
    else:
        # RGB/A image data (no colormap)
        fclim = Function(_apply_clim)
        fgamma = Function(_apply_gamma)
        fun = FunctionChain(None, [Function(_null_color_transform), fclim, fgamma])
    fclim['clim'] = clim
    fgamma['gamma'] = gamma
    return fun


class ImageVisual(Visual):
    """Visual subclass displaying an image.

    Parameters
    ----------
    data : ndarray
        ImageVisual data. Can be shape (M, N), (M, N, 3), or (M, N, 4).
    method : str
        Selects method of rendering image in case of non-linear transforms.
        Each method produces similar results, but may trade efficiency
        and accuracy. If the transform is linear, this parameter is ignored
        and a single quad is drawn around the area of the image.

            * 'auto': Automatically select 'impostor' if the image is drawn
              with a nonlinear transform; otherwise select 'subdivide'.
            * 'subdivide': ImageVisual is represented as a grid of triangles
              with texture coordinates linearly mapped.
            * 'impostor': ImageVisual is represented as a quad covering the
              entire view, with texture coordinates determined by the
              transform. This produces the best transformation results, but may
              be slow.

    grid: tuple (rows, cols)
        If method='subdivide', this tuple determines the number of rows and
        columns in the image grid.
    cmap : str | ColorMap
        Colormap to use for luminance images.
    clim : str | tuple
        Limits to use for the colormap. Can be 'auto' to auto-set bounds to
        the min and max of the data.
    gamma : float
        Gamma to use during colormap lookup.  Final color will be cmap(val**gamma).
        by default: 1.
    interpolation : str
        Selects method of image interpolation. Makes use of the two Texture2D
        interpolation methods and the available interpolation methods defined
        in vispy/gloo/glsl/misc/spatial_filters.frag

            * 'nearest': Default, uses 'nearest' with Texture2D interpolation.
            * 'bilinear': uses 'linear' with Texture2D interpolation.
            * 'hanning', 'hamming', 'hermite', 'kaiser', 'quadric', 'bicubic',
                'catrom', 'mitchell', 'spline16', 'spline36', 'gaussian',
                'bessel', 'sinc', 'lanczos', 'blackman'
    texture_format: numpy.dtype | str | None
        How to store data on the GPU. OpenGL allows for many different storage
        formats and schemes for the low-level texture data stored in the GPU.
        Most common is unsigned integers or floating point numbers.
        Unsigned integers are the most widely supported while other formats
        may not be supported on older versions of OpenGL, WebGL
        (without enabling some extensions), or with older GPUs.
        Default value is ``None`` which means data will be scaled on the
        CPU and the result stored in the GPU as an unsigned integer. If a
        numpy dtype object, an internal texture format will be chosen to
        support that dtype and data will *not* be scaled on the CPU. Not all
        dtypes are supported. If a string, then
        it must be one of the OpenGL internalformat strings described in the
        table on this page: https://www.khronos.org/registry/OpenGL-Refpages/gl4/html/glTexImage2D.xhtml
        The name should have `GL_` removed and be lowercase (ex.
        `GL_R32F` becomes ``'r32f'``). Lastly, this can also be the string
        ``'auto'`` which will use the data type of the provided image data
        to determine the internalformat of the texture.
        When this is specified (not ``None``) data is scaled on the
        GPU which allows for faster color limit changes. Additionally, when
        32-bit float data is provided it won't be copied before being
        transferred to the GPU.
    **kwargs : dict
        Keyword arguments to pass to `Visual`.

    Notes
    -----
    The colormap functionality through ``cmap`` and ``clim`` are only used
    if the data are 2D.
    """

    def __init__(self, data=None, method='auto', grid=(1, 1),
                 cmap='viridis', clim='auto', gamma=1.0,
                 interpolation='nearest', texture_format=None, **kwargs):
        self._data = None
        self._gamma = gamma

        # load 'float packed rgba8' interpolation kernel
        # to load float interpolation kernel use
        # `load_spatial_filters(packed=False)`
        kernel, self._interpolation_names = load_spatial_filters()

        self._kerneltex = Texture2D(kernel, interpolation='nearest')
        # The unpacking can be debugged by changing "spatial-filters.frag"
        # to have the "unpack" function just return the .r component. That
        # combined with using the below as the _kerneltex allows debugging
        # of the pipeline
        # self._kerneltex = Texture2D(kernel, interpolation='linear',
        #                             internalformat='r32f')

        # create interpolation shader functions for available
        # interpolations
        fun = [Function(_interpolation_template % n)
               for n in self._interpolation_names]
        self._interpolation_names = [n.lower()
                                     for n in self._interpolation_names]

        self._interpolation_fun = dict(zip(self._interpolation_names, fun))
        self._interpolation_names.sort()
        self._interpolation_names = tuple(self._interpolation_names)

        # overwrite "nearest" and "bilinear" spatial-filters
        # with  "hardware" interpolation _data_lookup_fn
        self._interpolation_fun['nearest'] = Function(_texture_lookup)
        self._interpolation_fun['bilinear'] = Function(_texture_lookup)

        if interpolation not in self._interpolation_names:
            raise ValueError("interpolation must be one of %s" %
                             ', '.join(self._interpolation_names))

        self._interpolation = interpolation

        # check texture interpolation
        if self._interpolation == 'bilinear':
            texture_interpolation = 'linear'
        else:
            texture_interpolation = 'nearest'

        self._method = method
        self._grid = grid
        self._need_texture_upload = True
        self._need_vertex_update = True
        self._need_colortransform_update = True
        self._need_interpolation_update = True
        self._texture = ScalableTexture2D(data, internalformat=texture_format,
                                          interpolation=texture_interpolation)
        self._subdiv_position = VertexBuffer()
        self._subdiv_texcoord = VertexBuffer()

        # impostor quad covers entire viewport
        vertices = np.array([[-1, -1], [1, -1], [1, 1],
                             [-1, -1], [1, 1], [-1, 1]],
                            dtype=np.float32)
        self._impostor_coords = VertexBuffer(vertices)
        self._null_tr = NullTransform()

        self._init_view(self)
        super(ImageVisual, self).__init__(vcode=VERT_SHADER, fcode=FRAG_SHADER)
        self.set_gl_state('translucent', cull_face=False)
        self._draw_mode = 'triangles'

        # define _data_lookup_fn as None, will be setup in
        # self._build_interpolation()
        self._data_lookup_fn = None

        self.clim = clim
        self.cmap = cmap
        if data is not None:
            self.set_data(data)
        self.freeze()

    def set_data(self, image):
        """Set the data

        Parameters
        ----------
        image : array-like
            The image data.
        texture_format : str or None

        """
        data = np.asarray(image)
        if _should_cast_to_f32(data.dtype):
            data = data.astype(np.float32)
        if self._data is None or self._data.shape != data.shape:
            self._need_vertex_update = True
        self._data = data
        self._need_texture_upload = True

    def view(self):
        v = Visual.view(self)
        self._init_view(v)
        return v

    def _init_view(self, view):
        # Store some extra variables per-view
        view._need_method_update = True
        view._method_used = None

    @property
    def clim(self):
        return self._texture.clim

    @clim.setter
    def clim(self, clim):
        if self._texture.set_clim(clim):
            self._need_texture_upload = True
        # shortcut so we don't have to rebuild the whole color transform
        if not self._need_colortransform_update:
            self.shared_program.frag['color_transform'][1]['clim'] = self._texture.clim_normalized
        self.update()

    @property
    def cmap(self):
        return self._cmap

    @cmap.setter
    def cmap(self, cmap):
        self._cmap = get_colormap(cmap)
        self._need_colortransform_update = True
        self.update()

    @property
    def gamma(self):
        """The gamma used when rendering the image."""
        return self._gamma

    @gamma.setter
    def gamma(self, value):
        """Set gamma used when rendering the image."""
        if value <= 0:
            raise ValueError("gamma must be > 0")
        self._gamma = float(value)
        # shortcut so we don't have to rebuild the color transform
        if not self._need_colortransform_update:
            self.shared_program.frag['color_transform'][2]['gamma'] = self._gamma
        self.update()

    @property
    def method(self):
        return self._method

    @method.setter
    def method(self, m):
        if self._method != m:
            self._method = m
            self._need_vertex_update = True
            self.update()

    @property
    def size(self):
        return self._data.shape[:2][::-1]

    @property
    def interpolation(self):
        return self._interpolation

    @interpolation.setter
    def interpolation(self, i):
        if i not in self._interpolation_names:
            raise ValueError("interpolation must be one of %s" %
                             ', '.join(self._interpolation_names))
        if self._interpolation != i:
            self._interpolation = i
            self._need_interpolation_update = True
            self.update()

    @property
    def interpolation_functions(self):
        return self._interpolation_names

    # The interpolation code could be transferred to a dedicated filter
    # function in visuals/filters as discussed in #1051
    def _build_interpolation(self):
        """Rebuild the _data_lookup_fn for different interpolations."""
        interpolation = self._interpolation
        self._data_lookup_fn = self._interpolation_fun[interpolation]
        self.shared_program.frag['get_data'] = self._data_lookup_fn

        # only 'bilinear' uses 'linear' texture interpolation
        if interpolation == 'bilinear':
            texture_interpolation = 'linear'
        else:
            # 'nearest' (and also 'bilinear') doesn't use spatial_filters.frag
            # so u_kernel and shape setting is skipped
            texture_interpolation = 'nearest'
            if interpolation != 'nearest':
                self.shared_program['u_kernel'] = self._kerneltex
                self._data_lookup_fn['shape'] = self._data.shape[:2][::-1]

        if self._texture.interpolation != texture_interpolation:
            self._texture.interpolation = texture_interpolation

        self._data_lookup_fn['texture'] = self._texture

        self._need_interpolation_update = False

    def _build_vertex_data(self):
        """Rebuild the vertex buffers for the subdivide method."""
        grid = self._grid
        w = 1.0 / grid[1]
        h = 1.0 / grid[0]

        quad = np.array([[0, 0, 0], [w, 0, 0], [w, h, 0],
                         [0, 0, 0], [w, h, 0], [0, h, 0]],
                        dtype=np.float32)
        quads = np.empty((grid[1], grid[0], 6, 3), dtype=np.float32)
        quads[:] = quad

        mgrid = np.mgrid[0.:grid[1], 0.:grid[0]].transpose(1, 2, 0)
        mgrid = mgrid[:, :, np.newaxis, :]
        mgrid[..., 0] *= w
        mgrid[..., 1] *= h

        quads[..., :2] += mgrid
        tex_coords = quads.reshape(grid[1]*grid[0]*6, 3)
        tex_coords = np.ascontiguousarray(tex_coords[:, :2])
        vertices = tex_coords * self.size

        self._subdiv_position.set_data(vertices.astype('float32'))
        self._subdiv_texcoord.set_data(tex_coords.astype('float32'))
        self._need_vertex_update = False

    def _update_method(self, view):
        """Decide which method to use for *view* and configure it accordingly.
        """
        method = self._method
        if method == 'auto':
            if view.transforms.get_transform().Linear:
                method = 'subdivide'
            else:
                method = 'impostor'
        view._method_used = method

        if method == 'subdivide':
            view.view_program['method'] = 0
            view.view_program['a_position'] = self._subdiv_position
            view.view_program['a_texcoord'] = self._subdiv_texcoord
        elif method == 'impostor':
            view.view_program['method'] = 1
            view.view_program['a_position'] = self._impostor_coords
            view.view_program['a_texcoord'] = self._impostor_coords
        else:
            raise ValueError("Unknown image draw method '%s'" % method)

        self.shared_program['image_size'] = self.size
        view._need_method_update = False
        self._prepare_transforms(view)

    def _build_texture(self):
        self._texture.set_data(self._data)
        # XXX: Does this *always* need a colortransform update?
        self._need_colortransform_update = True
        self._need_texture_upload = False

    def _compute_bounds(self, axis, view):
        if axis > 1:
            return (0, 0)
        else:
            return (0, self.size[axis])

    def _prepare_transforms(self, view):
        trs = view.transforms
        prg = view.view_program
        method = view._method_used
        if method == 'subdivide':
            prg.vert['transform'] = trs.get_transform()
            prg.frag['transform'] = self._null_tr
        else:
            prg.vert['transform'] = self._null_tr
            prg.frag['transform'] = trs.get_transform().inverse

    def _prepare_draw(self, view):
        if self._data is None:
            return False

        if self._need_interpolation_update:
            self._build_interpolation()

        if self._need_texture_upload:
            self._build_texture()

        if self._need_colortransform_update:
            prg = view.view_program
            self.shared_program.frag['color_transform'] = _build_color_transform(
                self._data, self._texture.clim_normalized, self.gamma, self.cmap
            )
            self._need_colortransform_update = False
            prg['texture2D_LUT'] = self.cmap.texture_lut() \
                if (hasattr(self.cmap, 'texture_lut')) else None

        if self._need_vertex_update:
            self._build_vertex_data()

        if view._need_method_update:
            self._update_method(view)
