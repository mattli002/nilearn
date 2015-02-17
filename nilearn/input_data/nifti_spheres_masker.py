"""
Transformer for computing seeds signals.
"""

import numpy as np

from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.externals.joblib import Memory

from .. import _utils
from .._utils import logger, CacheMixin
from .._utils.niimg_conversions import check_niimg
from .. import signal
from .. import image
from .. import masking


def _signals_from_seeds(seeds, niimg, radius=None, mask_img=None):
    """ Note: this function is sub-optimal for small radius
    """
    n_seeds = len(seeds)
    niimg = check_niimg(niimg)
    shape = niimg.get_data().shape
    affine = niimg.get_affine()
    if mask_img is not None:
        mask_img = check_niimg(mask_img, ensure_3d=True)
        mask_img = image.resample_img(mask_img, target_affine=affine,
                                      target_shape=shape,
                                      interpolation='nearest')
        mask, _ = masking._load_mask_img(mask_img)
    signals = np.empty((shape[3], n_seeds))
    # Create an array of shape (3, array.shape) containing the i, j, k indices
    # in voxel space
    coords = np.vstack((np.indices(shape[:3]),
                        np.ones((1,) + shape[:3])))
    # Transform the indices into native space
    coords = np.tensordot(affine, coords, axes=[[1], [0]])[:3]
    for i, seed in enumerate(seeds):
        seed = np.asarray(seed)
        # Compute square distance to the seed
        dist = ((coords - seed[:, None, None, None]) ** 2).sum(axis=0)
        if radius is None or radius ** 2 < np.min(dist):
            dist_mask = (dist == np.min(dist))
        else:
            dist_mask = (dist <= radius ** 2)
        if mask_img is not None:
            dist_mask = np.logical_and(mask, dist_mask)
        if not dist_mask.any():
            raise ValueError('Seed #%i is out of the mask' % i)
        signals[:, i] = np.mean(niimg.get_data()[dist_mask], axis=0)
    return signals


def _compose_err_msg(msg, **kwargs):
    """Append key-value pairs to msg, for display.

    Parameters
    ==========
    msg: string
        arbitrary message
    kwargs: dict
        arbitrary dictionary

    Returns
    =======
    updated_msg: string
        msg, with "key: value" appended. Only string values are appended.
    """
    updated_msg = msg
    for k, v in kwargs.iteritems():
        if isinstance(v, basestring):
            updated_msg += "\n" + k + ": " + v

    return updated_msg


class NiftiSpheresMasker(BaseEstimator, TransformerMixin, CacheMixin):
    """Class for masking of Niimg-like objects using seeds.

    NiftiSpheresMasker is useful when data from given seeds should be
    extracted. Use case: Summarize brain signals from seeds that were
    obtained from prior knowledge.

    Parameters
    ==========
    seeds: List of triplet of coordinates in native space
        Seed definitions. List of coordinates of the seeds in the same space
        as the images (typically MNI or TAL).

    radius: float, optional.
        Indicates, in millimeters, the radius for the sphere around the seed.
        Default is None (signal is extracted on a single voxel).

    smoothing_fwhm: float, optional
        If smoothing_fwhm is not None, it gives the full-width half maximum in
        millimeters of the spatial smoothing to apply to the signal.

    standardize: boolean, optional
        If standardize is True, the time-series are centered and normed:
        their mean is set to 0 and their variance to 1 in the time dimension.

    detrend: boolean, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    low_pass: False or float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    high_pass: False or float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    t_r: float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    memory: joblib.Memory or str, optional
        Used to cache the region extraction process.
        By default, no caching is done. If a string is given, it is the
        path to the caching directory.

    memory_level: int, optional
        Aggressiveness of memory caching. The higher the number, the higher
        the number of functions that will be cached. Zero means no caching.

    verbose: integer, optional
        Indicate the level of verbosity. By default, nothing is printed.

    See also
    ========
    nilearn.input_data.NiftiMasker
    """
    # memory and memory_level are used by CacheMixin.

    def __init__(self, seeds, radius=None, mask_img=None,
                 smoothing_fwhm=None, standardize=False, detrend=False,
                 low_pass=None, high_pass=None, t_r=None,
                 memory=Memory(cachedir=None, verbose=0), memory_level=1,
                 verbose=0):
        self.seeds = seeds
        self.mask_img = mask_img
        self.radius = radius

        # Parameters for _smooth_array
        self.smoothing_fwhm = smoothing_fwhm

        # Parameters for clean()
        self.standardize = standardize
        self.detrend = detrend
        self.low_pass = low_pass
        self.high_pass = high_pass
        self.t_r = t_r

        # Parameters for joblib
        self.memory = memory
        self.memory_level = memory_level

        self.verbose = verbose

    def fit(self, X=None, y=None):
        """Prepare signal extraction from regions.

        All parameters are unused, they are for scikit-learn compatibility.
        """
            # This is not elegant but this is the easiest way to test it.
        try:
            for seed in self.seeds:
                assert(len(seed) == 3)
        except Exception as e:
            if self.verbose > 0:
                print('Seeds not valid, error' + str(e))
            raise ValueError('Seeds must be a list of triplets of '
                             'coordinates in native space.')
        self.seeds_ = self.seeds
        return self

    def fit_transform(self, imgs, confounds=None):
        return self.fit().transform(imgs, confounds=confounds)

    def _check_fitted(self):
        if not hasattr(self, "seeds_"):
            raise ValueError('It seems that %s has not been fitted. '
                             'You must call fit() before calling transform().'
                             % self.__class__.__name__)

    def transform(self, imgs, confounds=None):
        """Extract signals from Nifti-like objects.

        Parameters
        ==========
        imgs: Niimg-like object
            See http://nilearn.github.io/building_blocks/manipulating_mr_images.html#niimg.
            Images to process. It must boil down to a 4D image with scans
            number as last dimension.

        confounds: array-like, optional
            This parameter is passed to signal.clean. Please see the related
            documentation for details.
            shape: (number of scans, number of confounds)

        Returns
        =======
        signals: 2D numpy.ndarray
            Signal for each region.
            shape: (number of scans, number of regions)

        """
        self._check_fitted()

        logger.log("loading images: %s" %
                   _utils._repr_niimgs(imgs)[:200], verbose=self.verbose)
        imgs = _utils.check_niimgs(imgs)

        if self.smoothing_fwhm is not None:
            logger.log("smoothing images", verbose=self.verbose)
            imgs = self._cache(image.smooth_img, func_memory_level=1)(
                imgs, fwhm=self.smoothing_fwhm)

        logger.log("extracting region signals", verbose=self.verbose)
        signals = self._cache(
            _signals_from_seeds, func_memory_level=1)(
                self.seeds_, imgs, radius=self.radius, mask_img=self.mask_img)

        logger.log("cleaning extracted signals", verbose=self.verbose)
        signals = self._cache(signal.clean, func_memory_level=1
                                     )(signals,
                                       detrend=self.detrend,
                                       standardize=self.standardize,
                                       t_r=self.t_r,
                                       low_pass=self.low_pass,
                                       high_pass=self.high_pass,
                                       confounds=confounds)
        return signals
