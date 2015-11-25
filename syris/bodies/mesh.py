"""Bodies made from mesh."""
import re
import numpy as np
import pyopencl as cl
import pyopencl.array as cl_array
import quantities as q
import syris.config as cfg
import syris.geometry as geom
from syris.bodies.base import MovableBody
from syris.util import get_magnitude, make_tuple


class Mesh(MovableBody):

    """Rigid Body based on *triangles* which form a polygon mesh. The triangles are a 2D array with
    shape (3, N), where N / 3 is the number of triangles. One polygon is formed by three consecutive
    triangles, e.g. when::

        triangles = [[Ax, Bx, Cx]
                     [Ay, By, Cy]
                     [Az, Bz, Cz]]

    then A, B, C are one triangle's points. *iterations* are the number of iterations within one
    pixel which try to find an intersection. *center* determines the center of the local
    coordinates, it can be one of 'bbox', 'gravity' or a (z, y, x) tuple specifying an arbitrary
    point.
    """

    def __init__(self, triangles, trajectory, material=None, orientation=geom.Y_AX, iterations=1,
                 center=None):
        """Constructor."""
        super(Mesh, self).__init__(trajectory, material=material, orientation=orientation)
        # Use homogeneous coordinates for easy matrix multiplication, i.e. the 4-th element is 1
        self._current = np.insert(triangles.rescale(q.um).magnitude, 3, np.ones(triangles.shape[1]), axis=0)
        if center is not None:
            if center == 'gravity':
                point = self.center_of_gravity
            elif center == 'bbox':
                point = self.center_of_bbox
            else:
                # Arbitrary point
                point = center
            point = np.insert(point.rescale(q.um).magnitude[::-1], 3, 0)[:, np.newaxis]
            self._current -= point
        self._triangles = np.copy(self._current)
        points = triangles - self.center_of_gravity[::-1][:, np.newaxis]
        self._furthest_point = np.max(np.sqrt(np.sum(points ** 2, axis=0)))
        self.iterations = iterations

    @property
    def furthest_point(self):
        """Furthest point from the center."""
        return self._furthest_point

    @property
    def bounding_box(self):
        """Bounding box implementation."""
        z, y, x = self.extrema

        return geom.BoundingBox(geom.make_points(x, y, z))

    @property
    def num_triangles(self):
        """Number of triangles in the mesh."""
        return self._current.shape[1] / 3

    @property
    def extrema(self):
        """Mesh extrema as ((z_min, z_max), (y_min, y_max), (x_min, x_max))."""
        return ((self._compute(min, 2), self._compute(max, 2)),
                (self._compute(min, 1), self._compute(max, 1)),
                (self._compute(min, 0), self._compute(max, 0))) * q.um

    @property
    def center_of_gravity(self):
        """Get body's center of gravity as (z, y, x)."""
        center = (self._compute(np.mean, 2), self._compute(np.mean, 1), self._compute(np.mean, 0))

        return np.array(center) * q.um

    @property
    def center_of_bbox(self):
        """The bounding box center."""
        def get_middle(ends):
            return (ends[0] + ends[1]) / 2.

        return np.array([get_middle(ends) for ends in self.extrema.magnitude]) * q.um

    @property
    def diff(self):
        """Smallest and greatest difference between all mesh points in all three dimensions. Returns
        ((min(dy), max(dz)), (min(dy), max(dy)), (min(dx), max(dx))).
        """
        func = lambda ar: np.abs(ar[1:] - ar[:-1])
        min_nonzero = lambda ar: min(ar[np.where(ar != 0)])
        max_nonzero = lambda ar: max(ar[np.where(ar != 0)])
        x_diff = self._compute(func, 0)
        y_diff = self._compute(func, 1)
        z_diff = self._compute(func, 2)

        return ((min_nonzero(z_diff), max_nonzero(z_diff)),
                (min_nonzero(y_diff), max_nonzero(y_diff)),
                (min_nonzero(x_diff), max_nonzero(x_diff))) * q.um

    @property
    def vectors(self):
        """The triangles as B - A and C - A vectors where A, B, C are the triangle vertices. The
        result is transposed, i.e. axis 1 are x, y, z coordinates.
        """
        a = self._current[:-1, 0::3]
        b = self._current[:-1, 1::3]
        c = self._current[:-1, 2::3]
        v_0 = (b - a).transpose()
        v_1 = (c - a).transpose()

        return v_0, v_1

    @property
    def areas(self):
        """Triangle areas."""
        v_0, v_1 = self.vectors
        cross = np.cross(v_0, v_1)

        return np.sqrt(np.sum(cross * cross, axis=1)) / 2

    @property
    def normals(self):
        """Triangle normals."""
        v_0, v_1 = self.vectors

        return np.cross(v_0, v_1)

    @property
    def max_triangle_x_diff(self):
        """Get the greatest x-distance between triangle vertices."""
        x_0 = self._current[0, 0::3]
        x_1 = self._current[0, 1::3]
        x_2 = self._current[0, 2::3]
        d_0 = np.max(np.abs(x_1 - x_0))
        d_1 = np.max(np.abs(x_1 - x_2))
        d_2 = np.max(np.abs(x_2 - x_1))

        return max(d_0, d_1, d_2)

    def sort(self):
        """Sort triangles based on the greatest x-coordinate in an ascending order. Also sort
        vertices inside the triangles so that the greatest one is the last one, however, the
        position of the two remaining ones is not sorted.
        """
        # Extract x-coordinates
        x = self._current[0, :].reshape(self.num_triangles, 3)
        # Get vertices with the greatest x-coordinate and scale the indices up so we can work with
        # the original array
        factor = np.arange(self.num_triangles) * 3
        representatives = np.argmax(x, axis=1) + factor
        # Get indices which sort the triangles
        base = 3 * np.argsort(self._current[0, representatives])
        indices = np.empty(3 * len(base), dtype=np.int)
        indices[::3] = base
        indices[1::3] = base + 1
        indices[2::3] = base + 2

        # Sort the triangles such that the largest x-coordinate is in the last vertex
        tmp = np.copy(self._current[:, 2::3])
        self._current[:, 2::3] = self._current[:, representatives]
        self._current[:, representatives] = tmp

        # Sort the triangles among each other
        self._current = self._current[:, indices]

    def get_degenerate_triangles(self, matrix, eps=1e-3, scale=None, offset=None):
        """Get triangles which are close to be parallel with the ray in z-direction. *matrix* is the
        transformation matrix applied to triangles, *eps* is the tolerance for the angle between a
        triangle and the ray to be still considered parallel in degrees. If *scale* and *offset* are
        provided, pixel indices are returned instead of real triangle coordinates.
        """
        ray = np.array([0, 0, 1, 1])
        i_matrix = np.linalg.inv(matrix)
        ray = np.dot(i_matrix, ray)[:-1]
        dot = np.sqrt(np.sum(self.normals ** 2, axis=1))
        theta = np.rad2deg(np.arccos(np.dot(self.normals, ray) / dot))
        diff = np.abs(theta - 90)
        indices = np.where(diff < eps)[0]
        close = np.dot(matrix, self._current[:, 3 * indices])
        if scale is not None and offset is not None:
            offset = np.array(offset)[:, np.newaxis]
            scale = np.array(scale)[:, np.newaxis]
            close[:-1, :] = (close[:-1, :] + 0.5 * scale - offset) / scale
            close = np.round(close).astype(np.int)

        return close

    def _compute(self, func, axis):
        """General function for computations with triangles."""
        return func(self._current[axis, :])

    def _make_vertices(self, index):
        """Make a flat array of vertices belong to *triangles* at *index*."""
        vertices = get_magnitude(self._current[:, index::3])

        return vertices.transpose().flatten().astype(cfg.PRECISION.np_float)

    def _make_inputs(self, queue):
        mf = cl.mem_flags
        v_1 = cl_array.to_device(queue, self._make_vertices(0))
        v_2 = cl_array.to_device(queue, self._make_vertices(1))
        v_3 = cl_array.to_device(queue, self._make_vertices(2))

        return v_1, v_2, v_3

    def transform(self):
        """Apply transformation *matrix* and return the resulting triangles."""
        # TODO: drop the inversion from MovableBody
        matrix = np.linalg.inv(self.get_rescaled_transform_matrix(q.um))
        self._current = np.dot(matrix.astype(self._triangles.dtype), self._triangles)

    def _project(self, shape, pixel_size, t=0 * q.s, queue=None, out=None):
        """Projection implementation."""
        def get_crop(index, fov):
            minimum = max(self.extrema[index][0], 0 * q.um)
            maximum = min(self.extrema[index][1], fov[index - 1])

            return minimum, maximum

        def get_dimension(minimum, maximum, index):
            return int(np.ceil(((maximum - minimum) / pixel_size[index]).simplified.magnitude))

        # Move to the desired location, apply the T matrix and resort the triangles
        # self.move(t)
        self.transform()
        self.sort()

        fov = shape * pixel_size
        if out is None:
            out = cl_array.zeros(queue, shape, dtype=cfg.PRECISION.np_float)

        if (self.extrema[2][0] < fov[1] and self.extrema[2][1] > 0 * q.um and
            self.extrema[1][0] < fov[0] and self.extrema[1][1] > 0 * q.um):
            # Object inside FOV
            x_min, x_max = get_crop(2, fov)
            y_min, y_max = get_crop(1, fov)
            width = min(get_dimension(x_min, x_max, 1), shape[1])
            height = min(get_dimension(y_min, y_max, 0), shape[0])
            offset = cl_array.vec.make_int2(get_magnitude(x_min / pixel_size[1]),
                                            get_magnitude(y_min / pixel_size[0]))
            v_1, v_2, v_3 = self._make_inputs(queue)
            max_dx = get_magnitude(self.max_triangle_x_diff)
            min_z = self.extrema[0][0].magnitude
            ps = pixel_size[0].rescale(q.um).magnitude

            cfg.OPENCL.programs['mesh'].compute_thickness(queue,
                                                          (width, height),
                                                          None,
                                                          v_1.data,
                                                          v_2.data,
                                                          v_3.data,
                                                          out.data,
                                                          np.int32(self.num_triangles),
                                                          np.int32(shape[1]),
                                                          offset,
                                                          cfg.PRECISION.np_float(ps),
                                                          cfg.PRECISION.np_float(max_dx),
                                                          cfg.PRECISION.np_float(min_z),
                                                          np.int32(self.iterations))

        return out

    def compute_slices(self, shape, pixel_size, num_slices):
        """Compute slices."""
        queue = cfg.OPENCL.queue
        pixel_size = make_tuple(pixel_size, num_dims=2)
        shape = make_tuple(shape, num_dims=2)
        v_1, v_2, v_3 = self._make_inputs(queue)
        offset = cl_array.vec.make_int2(0, 0)

        out_data = cl_array.zeros(queue, (num_slices,) + shape, dtype=np.uint8)
        max_dx = get_magnitude(self.max_triangle_x_diff)
        min_z = self.extrema[0][0].magnitude
        print out_data.shape

        cfg.OPENCL.programs['mesh'].compute_thickness(queue,
                                (shape[1], num_slices),
                                None,
                                v_1.data,
                                v_2.data,
                                v_3.data,
                                out_data.data,
                                np.int32(shape[0]),
                                np.int32(self.num_triangles),
                                offset,
                                cfg.PRECISION.np_float(get_magnitude(pixel_size[0])),
                                cfg.PRECISION.np_float(max_dx),
                                cfg.PRECISION.np_float(min_z))


        return out_data


def _extract_object(txt):
    """Extract an object from string *txt*."""
    face_start = txt.index('s ')
    if 'v' not in txt[face_start:]:
        obj_end = None
    else:
        obj_end = face_start + txt[face_start:].index('v')
    subtxt = txt[:obj_end]

    pattern = r'{} (?P<x>.*) (?P<y>.*) (?P<z>.*)'
    v_pattern = re.compile(pattern.format('v'))
    f_pattern = re.compile(pattern.format('f'))
    vertices = np.array(re.findall(v_pattern, subtxt)).astype(np.float32)
    faces = np.array(re.findall(f_pattern, subtxt)).astype(np.int32).flatten() - 1

    remainder = txt[obj_end:] if obj_end else None

    return remainder, vertices, faces


def read_blender_obj(filename, objects=None):
    """Read blender wavefront *filename*, extract only *objects* which are object indices."""
    remainder = open(filename, 'r').read()
    triangles = None
    face_start = 0
    i = 0

    while remainder:
        remainder, v, f = _extract_object(remainder)
        if objects is None or i in objects:
            if triangles is None:
                triangles = v[f - face_start].transpose()
            else:
                triangles = np.concatenate((triangles, v[f - face_start].transpose()), axis=1)
        face_start += len(v)
        i += 1

    return triangles