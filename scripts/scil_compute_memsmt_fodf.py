#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""
Script to compute Multishell Constrained Spherical Deconvolution ODFs.

By default, will output all possible files, using default names.
Specific names can be specified using the file flags specified in the
"File flags" section.

If --not_all is set, only the files specified explicitly by the flags
will be output.
"""

import argparse
import logging

from dipy.core.gradients import GradientTable
from dipy.data import get_sphere, default_sphere
from dipy.reconst import shm
from dipy.reconst.mcsd import MultiShellResponse, MultiShellDeconvModel
from dipy.sims.voxel import single_tensor
import nibabel as nib
import numpy as np

from scilpy.io.image import get_data_as_mask
from scilpy.io.utils import (add_overwrite_arg, assert_inputs_exist,
                             assert_outputs_exist, add_force_b0_arg,
                             add_sh_basis_args, add_processes_arg)
from scilpy.reconst.multi_processes import fit_from_model, convert_sh_basis
from scilpy.reconst.b_tensor_utils import generate_btensor_input, extract_affine


def _build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)

    p.add_argument('wm_frf',
                   help='Text file of WM response function.')
    p.add_argument('gm_frf',
                   help='Text file of GM response function.')
    p.add_argument('csf_frf',
                   help='Text file of CSF response function.')

    p.add_argument(
        '--input_linear', metavar='file', default=None,
        help='Path of the linear input diffusion volume.')
    p.add_argument(
        '--bvals_linear', metavar='file', default=None,
        help='Path of the linear bvals file, in FSL format.')
    p.add_argument(
        '--bvecs_linear', metavar='file', default=None,
        help='Path of the linear bvecs file, in FSL format.')
    p.add_argument(
        '--input_planar', metavar='file', default=None,
        help='Path of the planar input diffusion volume.')
    p.add_argument(
        '--bvals_planar', metavar='file', default=None,
        help='Path of the planar bvals file, in FSL format.')
    p.add_argument(
        '--bvecs_planar', metavar='file', default=None,
        help='Path of the planar bvecs file, in FSL format.')
    p.add_argument(
        '--input_spherical', metavar='file', default=None,
        help='Path of the spherical input diffusion volume.')
    p.add_argument(
        '--bvals_spherical', metavar='file', default=None,
        help='Path of the spherical bvals file, in FSL format.')
    p.add_argument(
        '--bvecs_spherical', metavar='file', default=None,
        help='Path of the spherical bvecs file, in FSL format.')
    p.add_argument(
        '--input_custom', metavar='file', default=None,
        help='Path of the custom input diffusion volume.')
    p.add_argument(
        '--bvals_custom', metavar='file', default=None,
        help='Path of the custom bvals file, in FSL format.')
    p.add_argument(
        '--bvecs_custom', metavar='file', default=None,
        help='Path of the custom bvecs file, in FSL format.')
    p.add_argument(
        '--bdelta_custom', type=float, choices=[0, 1, -0.5, 0.5],
        help='Value of the b_delta for the custom encoding.')

    p.add_argument(
        '--sh_order', metavar='int', default=8, type=int,
        help='SH order used for the CSD. (Default: 8)')
    p.add_argument(
        '--mask',
        help='Path to a binary mask. Only the data inside the '
             'mask will be used for computations and reconstruction.')
    p.add_argument(
        '--tolerance', type=int, default=20,
        help='The tolerated gap between the b-values to '
             'extract\nand the current b-value. [%(default)s]')

    p.add_argument(
        '--not_all', action='store_true',
        help='If set, only saves the files specified using the '
             'file flags. (Default: False)')

    add_force_b0_arg(p)
    add_sh_basis_args(p)
    add_processes_arg(p)

    g = p.add_argument_group(title='File flags')

    g.add_argument(
        '--wm_fodf', metavar='file', default='',
        help='Output filename for the WM ODF coefficients.')
    g.add_argument(
        '--gm_fodf', metavar='file', default='',
        help='Output filename for the GM ODF coefficients.')
    g.add_argument(
        '--csf_fodf', metavar='file', default='',
        help='Output filename for the CSF ODF coefficients.')
    g.add_argument(
        '--vf', metavar='file', default='',
        help='Output filename for the volume fractions map.')
    g.add_argument(
        '--vf_rgb', metavar='file', default='',
        help='Output filename for the volume fractions map in rgb.')

    add_overwrite_arg(p)

    return p

def single_tensor_btensor(gtab, evals, b_delta, S0=1):

    if b_delta > 1 or b_delta < -0.5:
        msg = """The value of b_delta must be between -0.5 and 1."""
        raise ValueError(msg)

    out_shape = gtab.bvecs.shape[:gtab.bvecs.ndim - 1]
    gradients = gtab.bvecs.reshape(-1, 3)

    evals = np.asarray(evals)
    D_iso = np.sum(evals) / 3.
    D_para = evals[np.argmax(abs(evals - D_iso))]
    D_perp = evals[np.argmin(abs(evals - D_iso))]
    D_delta = (D_para - D_perp) / (3 * D_iso)

    S = np.zeros(len(gradients))
    for (i, g) in enumerate(gradients):
        theta = np.arctan2(np.sqrt(g[0] **2 + g[1] ** 2), g[2])
        P_2 = (3 * np.cos(theta) ** 2 - 1) / 2.
        b = gtab.bvals[i]
        S[i] = S0 * np.exp(-b * D_iso * (1 + 2 * b_delta * D_delta * P_2))

    return S.reshape(out_shape)


def multi_shell_fiber_response(sh_order, bvals, wm_rf, gm_rf, csf_rf,
                               b_deltas=None, sphere=None, tol=20):
    bvals = np.array(bvals, copy=True)

    n = np.arange(0, sh_order + 1, 2)
    m = np.zeros_like(n)

    if sphere is None:
        sphere = default_sphere

    big_sphere = sphere.subdivide()
    theta, phi = big_sphere.theta, big_sphere.phi

    B = shm.real_sh_descoteaux_from_index(m, n, theta[:, None], phi[:, None])
    A = shm.real_sh_descoteaux_from_index(0, 0, 0, 0)

    if b_deltas is None:
        b_deltas = np.ones(len(bvals) - 1)

    response = np.empty([len(bvals), len(n) + 2])

    if bvals[0] < tol:
        gtab = GradientTable(big_sphere.vertices * 0)
        wm_response = single_tensor_btensor(gtab, wm_rf[0, :3], 1, wm_rf[0, 3])
        response[0, 2:] = np.linalg.lstsq(B, wm_response, rcond=None)[0]

        response[0, 1] = gm_rf[0, 3] / A
        response[0, 0] = csf_rf[0, 3] / A

        for i, bvalue in enumerate(bvals[1:]):
            gtab = GradientTable(big_sphere.vertices * bvalue)
            wm_response = single_tensor_btensor(gtab, wm_rf[i, :3], b_deltas[i], wm_rf[i, 3])
            response[i+1, 2:] = np.linalg.lstsq(B, wm_response, rcond=None)[0]

            response[i+1, 1] = gm_rf[i, 3] * np.exp(-bvalue * gm_rf[i, 0]) / A
            response[i+1, 0] = csf_rf[i, 3] * np.exp(-bvalue * csf_rf[i, 0]) / A

        S0 = [csf_rf[0, 3], gm_rf[0, 3], wm_rf[0, 3]]

    else:
        warnings.warn("""No b0 was given. Proceeding either way.""", UserWarning)
        for i, bvalue in enumerate(bvals):
            gtab = GradientTable(big_sphere.vertices * bvalue)
            wm_response = single_tensor_btensor(gtab, wm_rf[i, :3], b_deltas[i], wm_rf[i, 3])
            response[i, 2:] = np.linalg.lstsq(B, wm_response, rcond=None)[0]

            response[i, 1] = gm_rf[i, 3] * np.exp(-bvalue * gm_rf[i, 0]) / A
            response[i, 0] = csf_rf[i, 3] * np.exp(-bvalue * csf_rf[i, 0]) / A

        S0 = [csf_rf[0, 3], gm_rf[0, 3], wm_rf[0, 3]]

    return MultiShellResponse(response, sh_order, bvals, S0=S0)


def main():
    parser = _build_arg_parser() # !!!!!!!!!!!!!!!!!!!!!!!!! Add to parser : all linear input mandatory if input_linear is given (for example)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if not args.not_all:
        args.wm_fodf = args.wm_fodf or 'wm_fodf.nii.gz'
        args.gm_fodf = args.gm_fodf or 'gm_fodf.nii.gz'
        args.csf_fodf = args.csf_fodf or 'csf_fodf.nii.gz'
        args.vf = args.vf or 'vf.nii.gz'
        args.vf_rgb = args.vf_rgb or 'vf_rgb.nii.gz'

    arglist = [args.wm_fodf, args.gm_fodf, args.csf_fodf, args.vf, args.vf_rgb]
    if args.not_all and not any(arglist):
        parser.error('When using --not_all, you need to specify at least ' +
                     'one file to output.')

    assert_inputs_exist(parser, [],
                        optional=[args.input_linear, args.bvals_linear,
                                  args.bvecs_linear, args.input_planar,
                                  args.bvals_planar, args.bvecs_planar,
                                  args.input_spherical, args.bvals_spherical,
                                  args.bvecs_spherical])
    assert_outputs_exist(parser, args, arglist)

    # Loading data
    input_files = [args.input_linear, args.input_planar,
                            args.input_spherical, args.input_custom]
    bvals_files = [args.bvals_linear, args.bvals_planar,
                           args.bvals_spherical, args.bvals_custom]
    bvecs_files = [args.bvecs_linear, args.bvecs_planar, 
                           args.bvecs_spherical, args.bvecs_custom]
    b_deltas_list = [1.0, -0.5, 0, args.bdelta_custom]

    gtab, data, ubvals, ubdeltas = generate_btensor_input(input_files,
                                                          bvals_files,
                                                          bvecs_files,
                                                          b_deltas_list,
                                                          args.force_b0_threshold,
                                                          tol=args.tolerance)

    affine = extract_affine(input_files)

    wm_frf = np.loadtxt(args.wm_frf)
    gm_frf = np.loadtxt(args.gm_frf)
    csf_frf = np.loadtxt(args.csf_frf)

    # Checking mask
    if args.mask is None:
        mask = None
    else:
        mask = get_data_as_mask(nib.load(args.mask), dtype=bool)
        if mask.shape != data.shape[:-1]:
            raise ValueError("Mask is not the same shape as data.")

    sh_order = args.sh_order

    # Checking data and sh_order
    if data.shape[-1] < (sh_order + 1) * (sh_order + 2) / 2:
        logging.warning(
            'We recommend having at least {} unique DWIs volumes, but you '
            'currently have {} volumes. Try lowering the parameter --sh_order '
            'in case of non convergence.'.format(
                (sh_order + 1) * (sh_order + 2) / 2, data.shape[-1]))

    # Checking response functions and computing msmt response function
    if len(wm_frf.shape) == 1:
        wm_frf = np.reshape(wm_frf, (1,) + wm_frf.shape)
    if len(gm_frf.shape) == 1:
        gm_frf = np.reshape(gm_frf, (1,) + gm_frf.shape)
    if len(csf_frf.shape) == 1:
        csf_frf = np.reshape(csf_frf, (1,) + csf_frf.shape)

    if not wm_frf.shape[1] == 4:
        raise ValueError('WM frf file did not contain 4 elements. '
                         'Invalid or deprecated FRF format')
    if not gm_frf.shape[1] == 4:
        raise ValueError('GM frf file did not contain 4 elements. '
                         'Invalid or deprecated FRF format')
    if not csf_frf.shape[1] == 4:
        raise ValueError('CSF frf file did not contain 4 elements. '
                         'Invalid or deprecated FRF format')
    mdmsmt_response = multi_shell_fiber_response(sh_order,
                                                 ubvals,
                                                 wm_frf, gm_frf, csf_frf,
                                                 ubdeltas[1:],
                                                 tol=args.tolerance)

    reg_sphere = get_sphere('repulsion724')

    # Computing msmt-CSD
    mdmsmt_model = MultiShellDeconvModel(gtab, mdmsmt_response,
                                       reg_sphere=reg_sphere,
                                       sh_order=sh_order)

    # Computing msmt-CSD fit
    mdmsmt_fit = fit_from_model(mdmsmt_model, data,
                              mask=mask, nbr_processes=args.nbr_processes)

    # Saving results
    if args.wm_fodf:
        shm_coeff = mdmsmt_fit.shm_coeff
        if args.sh_basis == 'tournier07':
            shm_coeff = convert_sh_basis(shm_coeff, reg_sphere, mask=mask,
                                         nbr_processes=args.nbr_processes)
        nib.save(nib.Nifti1Image(shm_coeff.astype(np.float32),
                                    affine), args.wm_fodf)

    if args.gm_fodf:
        shm_coeff = mdmsmt_fit.all_shm_coeff[..., 1]
        if args.sh_basis == 'tournier07':
            shm_coeff = shm_coeff.reshape(shm_coeff.shape + (1,))
            shm_coeff = convert_sh_basis(shm_coeff, reg_sphere, mask=mask,
                                         nbr_processes=args.nbr_processes)
        nib.save(nib.Nifti1Image(shm_coeff.astype(np.float32),
                                    affine), args.gm_fodf)
                                
    if args.csf_fodf:
        shm_coeff = mdmsmt_fit.all_shm_coeff[..., 0]
        if args.sh_basis == 'tournier07':
            shm_coeff = shm_coeff.reshape(shm_coeff.shape + (1,))
            shm_coeff = convert_sh_basis(shm_coeff, reg_sphere, mask=mask,
                                         nbr_processes=args.nbr_processes)
        nib.save(nib.Nifti1Image(shm_coeff.astype(np.float32),
                                    affine), args.csf_fodf)

    if args.vf:
        nib.save(nib.Nifti1Image(mdmsmt_fit.volume_fractions.astype(np.float32),
                                 affine), args.vf)

    if args.vf_rgb:
        vf = mdmsmt_fit.volume_fractions
        vf_rgb = vf / np.max(vf) * 255
        vf_rgb = np.clip(vf_rgb, 0, 255)
        nib.save(nib.Nifti1Image(vf_rgb.astype(np.uint8),
                                 affine), args.vf_rgb)


if __name__ == "__main__":
    main()
