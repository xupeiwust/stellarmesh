"""Stellarmesh MOAB/DAGMC models.

name: moab.py
author: Alex Koen

desc: MOABModel class represents a MOAB model.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
import warnings
from dataclasses import dataclass, field
from functools import cached_property

import gmsh
import numpy as np
import pymoab.core
import pymoab.types
from pymoab.rng import Range

from .mesh import Mesh

logger = logging.getLogger(__name__)


class _MOABEntity:
    model: MOABModel
    handle: np.uint64

    def __init__(self, model: MOABModel, handle: np.uint64):
        self.model = model
        self.handle = handle

    def __eq__(self, other):
        return self.handle == other.handle

    def __hash__(self):
        return hash(self.handle)

    @property
    def id(self):
        """Global ID"""
        model = self.model
        return model._core.tag_get_data(model.id_tag, self.handle, flat=True)[0]


class DAGMCSurface(_MOABEntity):
    """DAGMC surface entity."""

    model: DAGMCModel

    @property
    def adjacent_volumes(self) -> list[DAGMCVolume]:
        """Get adjacent volumes.

        Returns:
            Adjacent volumes.
        """
        parent_entities = self.model._core.get_parent_meshsets(self.handle)
        return [DAGMCVolume(self.model, e) for e in parent_entities]

    @property
    def triangles(self) -> Range:
        """Get range of triangle elements."""
        return self.model._core.get_entities_by_type(self.handle, pymoab.types.MBTRI)


class DAGMCVolume(_MOABEntity):
    """DAGMC volume entity."""

    model: DAGMCModel

    @property
    def adjacent_surfaces(self) -> list[DAGMCSurface]:
        """Get adjacent surfaces.

        Returns:
            Adjacent surfaces.
        """
        child_entities = self.model._core.get_child_meshsets(self.handle)
        return [DAGMCSurface(self.model, e) for e in child_entities]

    @property
    def group_name(self) -> str:
        """Name of the group containing this volume."""
        for (_, group_name), volume_handles in self.model.groups.items():
            if self.handle in volume_handles:
                return group_name
        else:
            raise ValueError("DAGMC volume is not contained in a group.")

    @group_name.setter
    def group_name(self, name: str):
        model = self.model
        core = model._core

        existing_group = False
        for (group_handle, group_name), volume_handles in model.groups.items():
            if name == group_name:
                # Add volume to group matching specified name, unless the volume
                # is already in it
                if self.handle in volume_handles:
                    return
                core.add_entities(group_handle, [self.handle])
                existing_group = True

            elif self.handle in volume_handles:
                # Remove name from existing set
                core.remove_entities(group_handle, [self.handle])

        if not existing_group:
            # Create new group, add name/category tags, add entity
            group_handle = core.create_meshset()
            core.tag_set_data(model.category_tag, group_handle, "Group")
            core.tag_set_data(model.name_tag, group_handle, name)
            core.add_entities(group_handle, [self.handle])


@dataclass
class _Surface:
    """Internal class for surface sense handling."""

    handle: np.uint64
    forward_volume: np.uint64 = field(default=np.uint64(0))
    reverse_volume: np.uint64 = field(default=np.uint64(0))

    def sense_data(self) -> list[np.uint64]:
        """Get MOAB tag sense data.

        Returns:
            Sense data.
        """
        return [self.forward_volume, self.reverse_volume]


class MOABModel:
    """MOAB Model.

    This class holds a generic MOAB mesh, which could be a 2D surface mesh used
    in DAGMC, a 3D tetrahedral mesh, etc.
    """

    _core: pymoab.core.Core

    def __init__(self, core: pymoab.core.Core):
        """Initialize model from a pymoab core object.

        Args:
            core: Pymoab core.
        """
        self._core = core

    @cached_property
    def category_tag(self) -> pymoab.tag.Tag:
        return self._core.tag_get_handle(
            pymoab.types.CATEGORY_TAG_NAME,
            pymoab.types.CATEGORY_TAG_SIZE,
            pymoab.types.MB_TYPE_OPAQUE,
            pymoab.types.MB_TAG_SPARSE,
            create_if_missing=True,
        )

    @cached_property
    def name_tag(self) -> pymoab.tag.Tag:
        return self._core.tag_get_handle(
            pymoab.types.NAME_TAG_NAME,
            pymoab.types.NAME_TAG_SIZE,
            pymoab.types.MB_TYPE_OPAQUE,
            pymoab.types.MB_TAG_SPARSE,
            create_if_missing=True,
        )

    @cached_property
    def id_tag(self) -> pymoab.tag.Tag:
        # Default tag, does not need to be created
        return self._core.tag_get_handle(pymoab.types.GLOBAL_ID_TAG_NAME)

    @cached_property
    def geom_dimension_tag(self) -> pymoab.tag.Tag:
        return self._core.tag_get_handle(
            pymoab.types.GEOM_DIMENSION_TAG_NAME,
            1,
            pymoab.types.MB_TYPE_INTEGER,
            pymoab.types.MB_TAG_SPARSE,
            create_if_missing=True,
        )

    @cached_property
    def surf_sense_tag(self) -> pymoab.tag.Tag:
        return self._core.tag_get_handle(
            "GEOM_SENSE_2",
            2,
            pymoab.types.MB_TYPE_HANDLE,
            pymoab.types.MB_TAG_SPARSE,
            create_if_missing=True,
        )

    @cached_property
    def faceting_tol_tag(self) -> pymoab.tag.Tag:
        return self._core.tag_get_handle(
            "FACETING_TOL",
            1,
            pymoab.types.MB_TYPE_DOUBLE,
            pymoab.types.MB_TAG_SPARSE,
            create_if_missing=True,
        )

    @property
    def root_set(self) -> np.uint64:
        """Get handle of MOAB root entity set."""
        return self._core.get_root_set()

    @property
    def tets(self) -> Range:
        """Get range of tetrahedral elements."""
        return self._core.get_entities_by_type(
            self.root_set, pymoab.types.MBTET, recur=True
        )

    @property
    def triangles(self) -> Range:
        """Get range of triangle elements."""
        return self._core.get_entities_by_type(
            self.root_set, pymoab.types.MBTRI, recur=True
        )

    @classmethod
    def from_h5m(cls, h5m_file: str) -> MOABModel:
        """Initialize model from .h5m file.

        Args:
            h5m_file: File to load.

        Returns:
            Initialized model.
        """
        core = pymoab.core.Core()
        core.load_file(h5m_file)
        return cls(core)

    @classmethod
    def from_mesh(cls, mesh: Mesh) -> MOABModel:
        """Create MOAB model from mesh.

        Args:
            mesh: Mesh from which to build MOAB mesh.

        Returns:
            Initialized model.
        """
        core = pymoab.core.Core()
        with tempfile.NamedTemporaryFile(suffix=".vtk", delete=True) as mesh_file:
            with mesh:
                gmsh.write(mesh_file.name)
            core.load_file(mesh_file.name)
        return cls(core)

    @classmethod
    def read_file(cls, h5m_file: str) -> MOABModel:
        """Initialize model from .h5m file.

        Args:
            h5m_file: File to load.

        Returns:
            Initialized model.
        """
        warnings.warn(
            "The read_file method is deprecated. Use from_h5m instead.",
            FutureWarning,
            stacklevel=2,
        )
        return cls.from_h5m(h5m_file)

    def write(self, filename: str):
        """Write MOAB model to .h5m, .vtk, or other file.

        Args:
            filename: Filename with format-appropriate extension.
        """
        self._core.write_file(filename)


class DAGMCModel(MOABModel):
    """DAGMC Model."""

    def __init__(self, core: pymoab.core.Core):
        """Initialize DAGMC model from a pymoab Core object

        Args:
            core: Pymoab core.
        """
        super().__init__(core)

    @property
    def groups(self) -> dict[tuple[np.uint64, str], Range]:
        """Get dictionary mapping a tuple of (group handle, group name) to the
        corresponding range containing entity sets in the group."""

        # Determine mapping of (group name, group entity) to volume handles
        group_handles: Range = self._core.get_entities_by_type_and_tag(
            self.root_set,
            pymoab.types.MBENTITYSET,
            [self.category_tag],
            ["Group"],
        )
        groups = {}
        for group_handle in group_handles:
            # Get list of volume handles
            volume_handles: Range = self._core.get_entities_by_type_and_tag(
                group_handle, pymoab.types.MBENTITYSET, [self.category_tag], ["Volume"]
            )
            group_name: str = self._core.tag_get_data(
                self.name_tag, group_handle, flat=True
            )[0]
            groups[group_handle, group_name] = volume_handles
        return groups

    @staticmethod
    def make_watertight(
        input_filename: str,
        output_filename: str,
        binary_path: str = "make_watertight",
    ):
        """Make mesh watertight.

        Args:
            input_filename: Input .h5m filename.
            output_filename: Output watertight .h5m filename.
            binary_path: Path to make_watertight or default to find in path. Defaults to
            "make_watertight".
        """
        subprocess.run(
            [binary_path, input_filename, "-o", output_filename],
            check=True,
        )

    @classmethod
    def from_mesh(  # noqa: PLR0915
        cls,
        mesh: Mesh,
    ) -> DAGMCModel:
        """Compose DAGMC MOAB .h5m file from mesh.

        Args:
            mesh: Mesh from which to build DAGMC geometry.
        """
        core = pymoab.core.Core()
        model = cls(core)

        known_surfaces: dict[int, _Surface] = {}
        known_groups: dict[int, np.uint64] = {}

        with mesh:
            # Warn about volume elements being discarded
            _, element_tags, _ = gmsh.model.mesh.get_elements(3)
            if element_tags:
                logger.warning("Discarding volume elements from mesh.")

            volume_dimtags = gmsh.model.get_entities(3)
            volume_tags = [v[1] for v in volume_dimtags]
            for i, volume_tag in enumerate(volume_tags):
                # Add volume set
                volume_set_handle = core.create_meshset()
                global_id = volume_set_handle
                core.tag_set_data(model.id_tag, global_id, i)
                core.tag_set_data(model.geom_dimension_tag, volume_set_handle, 3)
                core.tag_set_data(model.category_tag, volume_set_handle, "Volume")

                # Add volume to its physical group, which stores metadata incl. material
                # TODO(akoen): should this be a parent-child relationship?
                # https://github.com/Thea-Energy/neutronics-cad/issues/2
                vol_groups = gmsh.model.get_physical_groups_for_entity(3, volume_tag)
                if (num_groups := len(vol_groups)) != 1:
                    raise ValueError(
                        f"Volume with tag {volume_tag} and global_id {global_id} "
                        f"belongs to {num_groups} physical groups, should be 1"
                    )

                if (vol_group := vol_groups[0]) not in known_groups:
                    mat_name = gmsh.model.get_physical_name(3, vol_group)
                    group_set = core.create_meshset()
                    core.tag_set_data(model.category_tag, group_set, "Group")
                    core.tag_set_data(model.name_tag, group_set, f"{mat_name}")
                    core.tag_set_data(model.id_tag, group_set, vol_group)
                    known_groups[vol_group] = group_set
                else:
                    group_set = known_groups[vol_group]

                core.add_entity(group_set, volume_set_handle)

                # Add surfaces to MOAB core, respecting surface sense.
                # Logic: Gmsh meshes volumes in order. When it gets to the first volume,
                # it points all adjacent surfaces normals outward. For each subsequent
                # volume, it points the surface normals outwards iff the surface hasn't
                # yet been encountered. Thus, the first time a surface is encountered,
                # the current volume has a forward sense and the second time a reverse
                # sense.
                adjacencies = gmsh.model.get_adjacencies(3, volume_tag)
                surface_tags = adjacencies[1]
                for surface_tag in surface_tags:
                    if surface_tag not in known_surfaces:
                        surface_set_handle = core.create_meshset()
                        surface = _Surface(handle=surface_set_handle)
                        surface.forward_volume = volume_set_handle
                        known_surfaces[surface_tag] = surface

                        core.tag_set_data(model.id_tag, surface.handle, surface_tag)
                        core.tag_set_data(model.geom_dimension_tag, surface.handle, 2)
                        core.tag_set_data(model.category_tag, surface.handle, "Surface")
                        core.tag_set_data(
                            model.surf_sense_tag,
                            surface.handle,
                            surface.sense_data(),
                        )

                        # Write surface to MOAB. STL export/import is very efficient.
                        with tempfile.NamedTemporaryFile(
                            suffix=".stl", delete=True
                        ) as stl_file:
                            group_tag = gmsh.model.add_physical_group(2, [surface_tag])
                            gmsh.write(stl_file.name)
                            gmsh.model.remove_physical_groups([(2, group_tag)])
                            core.load_file(stl_file.name, surface_set_handle)

                    else:
                        # Surface already has a forward volume, so this must be the
                        # reverse volume.
                        surface = known_surfaces[surface_tag]
                        surface.reverse_volume = volume_set_handle
                        core.tag_set_data(
                            model.surf_sense_tag,
                            surface.handle,
                            surface.sense_data(),
                        )

                    core.add_parent_child(volume_set_handle, surface.handle)

            all_entities = core.get_entities_by_handle(0)
            file_set = core.create_meshset()
            # TODO(akoen): faceting tol set to a random value
            # https://github.com/Thea-Energy/neutronics-cad/issues/5
            # faceting_tol required to be set for make_watertight, although its
            # significance is not clear
            core.tag_set_data(model.faceting_tol_tag, file_set, 1e-3)
            core.add_entities(file_set, all_entities)

            return model

    @classmethod
    def make_from_mesh(cls, mesh: Mesh) -> DAGMCModel:
        """Compose DAGMC MOAB .h5m file from mesh.

        Args:
            mesh: Mesh from which to build DAGMC geometry.
        """
        warnings.warn(
            "The make_from_mesh method is deprecated. Use from_mesh instead.",
            FutureWarning,
            stacklevel=2,
        )
        return cls.from_mesh(mesh)

    def _get_entities_of_geom_dimension(self, dim: int) -> list[np.uint64]:
        return self._core.get_entities_by_type_and_tag(
            0, pymoab.types.MBENTITYSET, self.geom_dimension_tag, [dim]
        )

    @property
    def surfaces(self) -> list[DAGMCSurface]:
        """Get surfaces in this model.

        Returns:
            Surfaces.
        """
        surface_handles = self._get_entities_of_geom_dimension(2)
        return [DAGMCSurface(self, h) for h in surface_handles]

    @property
    def volumes(self) -> list[DAGMCVolume]:
        """Get volumes in this model.

        Returns:
            Volumes.
        """
        volume_handles = self._get_entities_of_geom_dimension(3)
        return [DAGMCVolume(self, h) for h in volume_handles]
