from contextlib import contextmanager
from copy import deepcopy

import numpy as np
from numpy import linalg as npl
from meshpy import tet
from scipy.spatial import cKDTree

from mesh_sphere_packing import logger, TOL


@contextmanager
def redirect_tetgen_output(fname='./tet.log'):
    """Context manager to redirect stdout of TetGen subprocess to file `fname`."""
    import ctypes, io, os, sys

    libc = ctypes.CDLL(None)
    c_stdout = ctypes.c_void_p.in_dll(libc, 'stdout')

    def _redirect_stdout(to_fd):
        libc.fflush(c_stdout)
        sys.stdout.close()
        os.dup2(to_fd, original_stdout_fd)
        sys.stdout = io.TextIOWrapper(os.fdopen(original_stdout_fd, 'wb'))

    def extract_stats(f):
        """Returns string containing key metrics of the constructed mesh."""
        while True:
            l = f.readline()
            if 'Statistics:' in l.decode('ascii'):
                stats = [f.readline().decode('ascii') for i in range(11)]
                npoints, ntets, nfaces, nedges = [
                    int(sl.split()[-1]) for sl in stats[7:]
                ]
                return 'Built mesh with {} points, {} tetrahedra, {} faces, and {} edges'\
                    .format(npoints, ntets, nfaces, nedges)

    logger.info('    -> calling TetGen (writing log to {})'.format(fname))

    original_stdout_fd = sys.stdout.fileno()
    saved_stdout_fd = os.dup(original_stdout_fd)
    try:
        tfile = open(fname, mode='w+b')
        _redirect_stdout(tfile.fileno())
        yield
        _redirect_stdout(saved_stdout_fd)
        tfile.seek(0, io.SEEK_SET)
        logger.info(extract_stats(tfile))
        tfile.close()
    finally:
        tfile.close()
        os.close(saved_stdout_fd)


def write_msh(fname, mesh):
    """Outputs mesh in Gmsh format. Details of Gmsh format available here:
    http://gmsh.info/doc/texinfo/gmsh.html#File-formats
    :param fname str: file path for mesh output.
    :param mesh MeshInfo: tetrahedral mesh.
    """
    points = np.array(mesh.points, dtype=np.float64)
    faces = np.array(mesh.faces, dtype=np.int32)
    markers = np.array(mesh.face_markers, dtype=np.int32)
    elements = np.array(mesh.elements, dtype=np.int32)

    faces += 1
    elements += 1

    with open(fname, 'w') as f:
        # Write header data
        f.write('$MeshFormat\n2.2 0 8\n$EndMeshFormat\n')

        # Write node data
        f.write('$Nodes\n%d\n' % (len(points)))
        for i, p in enumerate(points):
            f.write('%d %+1.15e %+1.15e %+1.15e\n' % (i+1, *p,))
        f.write('$EndNodes\n')

        # Write face data
        f.write('$Elements\n%d\n' % (len(faces)))
        for i, (fac, m) in enumerate(zip(faces, markers)):
            f.write('%d 2 2 %d 0 %d %d %d\n' % (i+1, m if m > 0 else -1, *fac,))
        f.write('$EndElements\n')


def write_ply(fname, mesh):
    """Outputs mesh in ply format. Details of ply format available here:
    http://paulbourke.net/dataformats/ply/
    :param fname str: file path for mesh output.
    :param mesh MeshInfo: tetrahedral mesh.
    """
    points, faces = list(mesh.points), list(mesh.faces)

    with open(fname, 'w') as f:
        # Write header data
        f.write('ply\nascii 1.0\n')
        f.write('element vertex %d\n' % len(points))
        f.write('property float x\nproperty float y\nproperty float z\n')
        f.write('element face %d\n' % len(faces))
        f.write('property list uchar int vertex_index\n')
        f.write('end_header\n')

        # Write node data
        for p in points:
            f.write('%+1.15e %+1.15e %+1.15e\n' % (*p,))

        # Write face data
        for fac in faces:
            f.write('3 %d %d %d\n' % (*fac,))


def write_poly(fname, mesh):
    """Outputs mesh in tetgen poly format. Details of tetgen poly format available here:
    http://wias-berlin.de/software/tetgen/fformats.poly.html
    :param fname str: file path for mesh output.
    :param mesh MeshInfo: tetrahedral mesh.
    """
    points, faces, markers, holes = list(mesh.points), list(mesh.faces),\
        list(mesh.face_markers), list(mesh.holes)
    with open(fname, 'w') as f:
        f.write('%d 3 0 1\n' % len(points))
        for i, p in enumerate(points):
            f.write('%5d %+1.15e %+1.15e %+1.15e\n' % (i, p[0], p[1], p[2]))
        f.write('%d 1\n' % len(faces))
        for i, (fac, m) in enumerate(zip(faces, markers)):
            f.write('1 0 %d\n%d %d %d %d\n' % (m, 3, fac[0], fac[1], fac[2]))
        if len(holes):
            f.write('%d\n' % len(holes))
            for i, h in enumerate(holes):
                f.write('%5d %+1.15e %+1.15e %+1.15e\n' % (i, h[0], h[1], h[2]))
        else:
            f.write('0\n')


def write_multiflow(fname, mesh):
    """Outputs mesh in multiflow format.
    WARNING: This method is slow! See work on the branch below for a C implementation:
    https://github.com/chrisk314/mesh-sphere-packing/tree/fea-write-mfmesh-c
    :param fname str: file path for mesh output.
    :param mesh MeshInfo: tetrahedral mesh.
    """
    from collections import defaultdict
    import h5py as h5

    points, elements, faces, markers, neighbours, adjacent_elements =\
        np.array(mesh.points), np.array(mesh.elements), np.array(mesh.faces),\
        np.array(mesh.face_markers), np.array(mesh.neighbors),\
        np.array(mesh.adjacent_elements)

    with h5.File(fname, 'w') as f:
        # Write node data.
        f['nodes'] = points.flatten()

        # Write cell data.
        cell_type = np.full((len(elements),1), 6)
        f['cells'] = np.hstack((cell_type, elements)).flatten()
        f['cellNodePtr'] = np.append([0], np.full(len(elements), 4).cumsum())

        # Write face data
        f['faces'] = faces.flatten()
        f['faceNodePtr'] = np.append([0], np.full(len(faces), 3).cumsum())

        # Write cell face connectivity data
        cell_faces = defaultdict(list)
        for idx, adj in enumerate(adjacent_elements):
            cell_faces[adj[0]].append(idx)
            cell_faces[adj[1]].append(idx)
        cell_faces.pop(-1)
        f['cellFaces'] = np.array(
            [item[1] for item in sorted(cell_faces.items(), key=lambda x: x[0])]
        ).flatten()

        f['cellFacePtr'] = np.append([0], np.full(len(elements), 4).cumsum())

        # Write cell neighbour data
        cell_nbr = neighbours[neighbours > -1]
        f['cellNeighbours'] = cell_nbr

        cell_nbr_ptr = np.empty(len(elements)+1, dtype=np.int32)
        cell_nbr_ptr[0] = 0
        cell_nbr_ptr[1:] = np.apply_along_axis(
            lambda x: np.where(x > -1)[0].shape[0], 1, neighbours
        ).cumsum()
        f['cellNeighbourPtr'] = cell_nbr_ptr

        # Write boundary marker data
        f['boundaryType'] = markers

        # Write header data.
        f['meshData'] = np.array([
            len(points),
            len(elements),
            len(faces),
            8,
            4 * len(elements),
            3 * len(faces),
            4 * len(elements),
            cell_nbr_ptr[-1]
        ])


def duplicate_lower_boundaries(domain, lower_boundaries):
    """Duplicates geometry and topology of lower boundaries at upper bound
    to ensure congruence of boundary triangles across PBCs.
    :param domain Domain: spatial domain for mesh.
    :param lower_boundaries list: list of lower bound boundaryPLC objects.
    :return: list of lower and upper bound PSLG structures.
    :rtype: list.
    """
    upper_boundaries = deepcopy(lower_boundaries)
    for i, ub in enumerate(upper_boundaries):
        translate = np.array([[domain.L[j] if j==i else 0. for j in range(3)]])
        ub.points += translate
    return lower_boundaries + upper_boundaries


def build_point_list(domain, sphere_pieces, boundaries):
    """Constructs full list of vertices for all geometry in the domain without
    duplicates. Reindexes all topology after vertex removal. This step is expensive
    but necessary as duplicate vertices cause segfaults in TetGen.
    :param domain Domain: spatial domain for mesh.
    :param sphere_pieces list: list of SpherePiece objects.
    :param boundaries list: list of boundaryPLC objects.
    :return: array of all vertices without duplicates.
    :rtype: numpy.ndarray.
    """
    logger.info('    -> building vertex list...')

    vcount = 0
    piece_points = []
    for points, tris in [(p.points, p.tris) for p in sphere_pieces]:
        piece_points.append(points)
        tris += vcount
        vcount += len(points)
    if len(piece_points):
        piece_points = np.vstack(piece_points)
    else:
        piece_points = np.empty((0,3), dtype=np.float64)

    on_x_lower = np.isclose(piece_points[:,0], 0.)
    on_y_lower = np.isclose(piece_points[:,1], 0.)
    on_z_lower = np.isclose(piece_points[:,2], 0.)
    on_x_upper = np.isclose(piece_points[:,0], domain.L[0])
    on_y_upper = np.isclose(piece_points[:,1], domain.L[1])
    on_z_upper = np.isclose(piece_points[:,2], domain.L[2])

    bpp_idx = np.where(
        on_x_lower | on_y_lower | on_z_lower |
        on_x_upper | on_y_upper | on_z_upper
    )[0]
    remap = {child: parent for child, parent in enumerate(bpp_idx)}

    vcount = len(bpp_idx)
    boundary_points = []
    for b in boundaries:
        boundary_points.append(b.points)
        b.tris += vcount
        vcount += len(b.points)
    boundary_points = np.vstack([piece_points[bpp_idx]] + boundary_points)
    mask = np.full(len(boundary_points), True)

    # Find duplicated vertices
    tree = cKDTree(boundary_points)
    _dup = sorted(tree.query_pairs(TOL), key=lambda x: x[0])

    dup = {}
    for k, v in _dup:
        if not v in dup:
            dup[v] = k
    del _dup

    # Remove duplicated vertices
    mask[list(dup.keys())] = False
    boundary_points = boundary_points[mask]

    # Reindex triangles
    vcount_bpp = len(bpp_idx)
    vcount_piece = len(piece_points)
    remap.update({
        v + vcount_bpp: v + vcount_piece for v
        in range(len(boundary_points) - len(bpp_idx))
    })

    reindex = {old: new for new, old in enumerate(np.where(mask)[0])}
    reindex.update({k: reindex[v] for k, v in dup.items()})
    del dup

    for b in boundaries:
        b.tris[:] = np.array([
            remap[reindex[v]] for v in b.tris.flatten()
        ]).reshape(b.tris.shape)

    return np.vstack((piece_points, boundary_points[vcount_bpp:]))


def build_facet_list(sphere_pieces, boundaries):
    """Constructs list of facets for tetgen mesh build.
    :param sphere_pieces list: list of SpherePiece objects.
    :param boundaries list: list of boundaryPLC objects.
    :return: tuple containing array of facet vertices and facet markers.
    :rtype: tuple.
    """
    all_facets = [b.tris for b in boundaries]
    all_markers = [
        np.full(len(all_facets[0]), 1), np.full(len(all_facets[1]), 3),
        np.full(len(all_facets[2]), 5), np.full(len(all_facets[3]), 2),
        np.full(len(all_facets[4]), 4), np.full(len(all_facets[5]), 6),
    ]
    mark_offset = 7
    for p in sphere_pieces:
        all_facets.append(p.tris)
        all_markers.append(np.full(len(p.tris), p.sphere.id + mark_offset))
    return np.vstack(all_facets), np.hstack(all_markers)


def build_hole_list(sphere_pieces):
    """Constructs list of hole points for tetgen mesh build.
    :param sphere_pieces list: list of SpherePiece objects.
    :return: array of hole point coordinates.
    :rtype: numpy.ndarray.
    """
    all_holes = [p.sphere.x for p in sphere_pieces if p.is_hole]
    if len(all_holes):
        return np.vstack(all_holes)
    return np.empty((0,3), dtype=np.float64)


def build_tetmesh(domain, sphere_pieces, boundaries, config):
    """Handles calling TetGen to construct the tetrahedral mesh.
    :param domain Domain: spatial domain for mesh.
    :param sphere_pieces list: list of SpherePiece objects.
    :param boundaries list: list of boundaryPLC objects.
    :param config Config: configuration for mesh build.
    :return mesh: tetrahedral mesh.
    :rtype: MeshInfo.
    """
    logger.info('Building tetrahedral mesh')

    boundaries = duplicate_lower_boundaries(domain, boundaries)

    points = build_point_list(domain, sphere_pieces, boundaries)

    # Fix boundary points to exactly zero
    for i in range(3):
        points[(np.isclose(points[:,i], 0.), i)] = 0.

    facets, markers = build_facet_list(sphere_pieces, boundaries)
    holes = build_hole_list(sphere_pieces)

    rad_edge = config.tetgen_rad_edge_ratio
    min_angle = config.tetgen_min_angle
    max_volume = config.tetgen_max_volume

    options = tet.Options('pq{}/{}nzfennYCV'.format(rad_edge, min_angle))
    options.quiet = False

    mesh = tet.MeshInfo()
    mesh.set_points(points)
    mesh.set_facets(facets.tolist(), markers=markers.tolist())
    mesh.set_holes(holes)

    with redirect_tetgen_output():
        return tet.build(
            mesh, options=options, verbose=True, max_volume=max_volume
        )
