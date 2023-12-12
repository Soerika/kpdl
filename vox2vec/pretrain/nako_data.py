import random
import nibabel
import numpy as np
from typing import *
from pathlib import Path
from imops import crop_to_box

import torch
from torch.utils.data import Dataset

from connectome import Source, meta, Chain, Apply, Transform, CacheToDisk

from vox2vec.utils.intensity_augmentations import AppearanceTransform
from vox2vec.processing import (
    BODY_THRESHOLD_MRI,
    sample_box,
    get_body_mask,
    gaussian_filter,
    gaussian_sharpen
)


class NAKOSource(Source):
    _root: str

    @meta
    def train_ids(_root):
        return sorted({
            file.name
            for file in Path(_root).glob('*2_3D_GRE_TRA_W*.nii.gz')
        })

    def _image_nii(id_, _root):
        file,  = Path(_root).glob(f'{id_}')
        return nibabel.load(file)

    def image(_image_nii):
        return _image_nii.get_fdata().astype(np.float32)

    def affine(_image_nii):
        return _image_nii.affine


class NAKODataset(Dataset):
    def __init__(
            self,
            cache_dir: str,
            patch_size: Tuple[int, int, int],
            max_num_voxels_per_patch: int,
            batch_size: int,
            data_dir: str,
    ) -> None:

        source = NAKOSource(root=data_dir)

        # use connectome for smart cashing
        preprocessing = Chain(
            Transform(__inherit__=True, body_voxels=lambda image: np.argwhere(get_body_mask(image, BODY_THRESHOLD_MRI)))
        )

        pipeline = source >> preprocessing >> CacheToDisk.simple('image', 'body_voxels', root=cache_dir)

        self.ids = source.train_ids
        self.load_example = pipeline._compile(['image', 'body_voxels'])
        self.patch_size = patch_size
        self.max_num_voxels_per_patch = max_num_voxels_per_patch
        self.batch_size = batch_size

        self.style_aug = AppearanceTransform(
            local_rate=0.8,
            nonlinear_rate=0.9,
            paint_rate=0.9,
            inpaint_rate=0.2
        )

    def __len__(self):
        return len(self.ids)

    def __getitem__(self, i):
        args = [*self.load_example(self.ids[i]), self.patch_size, self.max_num_voxels_per_patch, self.style_aug]
        views = [sample_views(*args) for _ in range(self.batch_size)]
        patches_1_aug, patches_2_aug, patches_1, patches_2, voxels_1, voxels_2 = zip(*views)
        patches_1 = torch.tensor(np.stack([p[None] for p in patches_1]))
        patches_2 = torch.tensor(np.stack([p[None] for p in patches_2]))
        patches_1_aug = torch.tensor(np.stack([p[None] for p in patches_1_aug]))
        patches_2_aug = torch.tensor(np.stack([p[None] for p in patches_2_aug]))
        voxels_1 = [torch.tensor(voxels) for voxels in voxels_1]
        voxels_2 = [torch.tensor(voxels) for voxels in voxels_2]
        return patches_1_aug, patches_2_aug, patches_1, patches_2, voxels_1, voxels_2


def sample_views(
        image: np.ndarray,
        roi_voxels: np.ndarray,
        patch_size: Tuple[int, int, int],
        max_num_voxels: int,
        style_aug
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    anchor_voxel = random.choice(roi_voxels)  # (3,)

    patch_1_aug, patch_1, roi_voxels_1 = sample_view(image, roi_voxels, anchor_voxel, patch_size, style_aug)
    patch_2_aug, patch_2, roi_voxels_2 = sample_view(image, roi_voxels, anchor_voxel, patch_size, style_aug)

    valid_1 = np.all((roi_voxels_1 >= 0) & (roi_voxels_1 < patch_size), axis=1)
    valid_2 = np.all((roi_voxels_2 >= 0) & (roi_voxels_2 < patch_size), axis=1)
    valid = valid_1 & valid_2
    assert valid.any()
    indices = np.where(valid)[0]

    if len(indices) > max_num_voxels:
        indices = np.random.choice(indices, max_num_voxels, replace=False)

    return patch_1_aug, patch_2_aug, patch_1, patch_2, roi_voxels_1[indices], roi_voxels_2[indices]


def sample_view(image, voxels, anchor_voxel, patch_size, style_aug):
    assert image.ndim == 3

    # spatial augmentations: random rescale, rotation and crop
    box = sample_box(image.shape, patch_size, anchor_voxel)
    image = crop_to_box(image, box, axis=(-3, -2, -1))
    shift = box[0]
    voxels = voxels - shift

    # intensity augmentations
    # if random.uniform(0, 1) < 0.5:
    #     if random.uniform(0, 1) < 0.5:
    #         # random gaussian blur in axial plane
    #         sigma = random.uniform(0.25, 1.5)
    #         image = gaussian_filter(image, sigma, axis=(0, 1))
    #     else:
    #         # random gaussian sharpening in axial plane
    #         sigma_1 = random.uniform(0.5, 1.0)
    #         sigma_2 = 0.5
    #         alpha = random.uniform(10.0, 30.0)
    #         image = gaussian_sharpen(image, sigma_1, sigma_2, alpha, axis=(0, 1))
    #
    # if random.uniform(0, 1) < 0.5:
    #     sigma_hu = random.uniform(0, 0.1)
    #     image = image + np.random.normal(0, sigma_hu, size=image.shape).astype('float32')

    image_aug = style_aug.rand_aug(image.copy())

    return image_aug, image, voxels
