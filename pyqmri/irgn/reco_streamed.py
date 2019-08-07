#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import division

import numpy as np
import time
import sys
from pkg_resources import resource_filename
import pyopencl.array as clarray
import h5py
import pyqmri.operator as operator
import pyqmri.streaming as streaming
from pyqmri._helper_fun import CLProgram as Program
DTYPE = np.complex64
DTYPE_real = np.float32


class ModelReco:
    def __init__(self, par, trafo=1, imagespace=False, SMS=False):
        par["overlap"] = 1
        self.overlap = par["overlap"]
        self.par_slices = par["par_slices"]
        self.par = par
        self.C = np.require(
            np.transpose(par["C"], [1, 0, 2, 3]), requirements='C',
            dtype=DTYPE)
        self.unknowns_TGV = par["unknowns_TGV"]
        self.unknowns_H1 = par["unknowns_H1"]
        self.unknowns = par["unknowns"]
        self.NSlice = par["NSlice"]
        self.NScan = par["NScan"]
        self.dimX = par["dimX"]
        self.dimY = par["dimY"]
        self.scale = 1

        self.N = par["N"]
        self.Nproj = par["Nproj"]
        self.dz = 1
        self.fval_old = 0
        self.fval = 0
        self.fval_init = 0
        self.SNR_est = par["SNR_est"]
        self.ctx = par["ctx"]
        self.queue = par["queue"]
        self.gn_res = []
        self.num_dev = len(par["num_dev"])
        if (self.NSlice/(self.num_dev*self.par_slices) < 2):
            raise ValueError(
                "Number of Slices devided by parallel "
                "computed slices and devices needs to be larger two.")
        if self.NSlice % self.par_slices:
            raise ValueError(
                "Number of Slices devided by parallel "
                "computed slices needs to be an integer.")
        self.prg = []
        for j in range(self.num_dev):
            self.prg.append(
                Program(
                    self.ctx[j],
                    open(
                        resource_filename(
                            'pyqmri',
                            'kernels/OpenCL_Kernels_streamed.c')).read()))

        self.ratio = []
        self.tmp_img = []

        self.unknown_shape = (self.NSlice, self.unknowns, self.dimY, self.dimX)
        self.grad_shape = self.unknown_shape + (4,)
        if imagespace:
            self.data_shape = (self.NSlice, self.NScan,
                               self.dimY, self.dimX)
            self.C = []
            self.NC = 1
            self.N = self.dimX
            self.Nproj = self.dimY
            self.dat_trans_axes = [1, 0, 2, 3]
            self.op = operator.OperatorImagespaceStreamed(par, self.prg)
            self.calc_residual = self.calc_residual_imagespace
            self.irgn_solve_3D = self.irgn_solve_3D_imagespace
        else:
            self.NC = par["NC"]
            self.dat_trans_axes = [2, 0, 1, 3, 4]
            if SMS:
                self.packs = par["packs"]
                self.MB = int(par["MB"])
                self.numofpacks = par["numofpacks"]
                self.data_shape = (self.packs*self.numofpacks, self.NScan,
                                   self.NC, self.Nproj, self.N)
                self.data_shape_T = (self.NScan, self.NC,
                                     self.packs*self.numofpacks,
                                     self.Nproj, self.N)
                self.op = operator.OperatorKspaceSMSStreamed(par,
                                                             self.prg,
                                                             trafo)
                self.tgv_solve_3D = self.tgv_solve_3DSMS
                self._setupstreamingops = self._setupstreamingopsSMS
                self.irgn_solve_3D = self.irgn_solve_3D_kspaceSMS
                self.calc_residual = self.calc_residual_kspaceSMS
            else:
                self.data_shape = (self.NSlice, self.NScan,
                                   self.NC, self.Nproj, self.N)
                self.op = operator.OperatorKspaceStreamed(par,
                                                          self.prg,
                                                          trafo)
                self.irgn_solve_3D = self.irgn_solve_3D_kspace
                self.calc_residual = self.calc_residual_kspace

        for j in range(self.num_dev):
            self.ratio.append(
                clarray.to_device(
                    self.queue[4*j],
                    (np.ones(self.unknowns)).astype(dtype=DTYPE_real)))

    def f_grad(self, outp, inp, par=[], idx=0, idxq=0,
               bound_cond=0, wait_for=[]):
        return self.prg[idx].gradient(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX),
            None, outp.data, inp[0].data,
            np.int32(self.unknowns),
            self.ratio[idx].data, np.float32(self.dz),
            wait_for=outp.events + inp[0].events + wait_for)

    def bdiv(self, outp, inp, par=[], idx=0, idxq=0,
             bound_cond=0, wait_for=[]):
        return self.prg[idx].divergence(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX), None,
            outp.data, inp[0].data, np.int32(self.unknowns),
            self.ratio[idx].data, np.int32(bound_cond), np.float32(self.dz),
            wait_for=outp.events + inp[0].events + wait_for)

    def sym_grad(self, outp, inp, par=[], idx=0, idxq=0,
                 bound_cond=0, wait_for=[]):
        return self.prg[idx].sym_grad(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX), None,
            outp.data, inp[0].data, np.int32(self.unknowns),
            np.float32(self.dz),
            wait_for=outp.events + inp[0].events + wait_for)

    def sym_bdiv(self, outp, inp, par=[], idx=0, idxq=0,
                 bound_cond=0, wait_for=[]):
        return self.prg[idx].sym_divergence(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX), None,
            outp.data, inp[0].data,
            np.int32(self.unknowns), np.int32(bound_cond), np.float32(self.dz),
            wait_for=outp.events + inp[0].events + wait_for)

    def update_Kyk2(self, outp, inp, par=[], idx=0, idxq=0,
                    bound_cond=0, wait_for=[]):
        return self.prg[idx].update_Kyk2(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX), None,
            outp.data, inp[0].data, inp[1].data,
            np.int32(self.unknowns), np.int32(bound_cond), np.float32(self.dz),
            wait_for=outp.events + inp[0].events + inp[1].events+wait_for)

    def update_primal(self, outp, inp, par=[], idx=0, idxq=0,
                      bound_cond=0, wait_for=[]):
        return self.prg[idx].update_primal(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX), None,
            outp.data, inp[0].data, inp[1].data, inp[2].data,
            np.float32(par[0]),
            np.float32(par[0]/par[1]), np.float32(1/(1+par[0]/par[1])),
            self.min_const[idx].data, self.max_const[idx].data,
            self.real_const[idx].data, np.int32(self.unknowns),
            wait_for=(outp.events +
                      inp[0].events+inp[1].events +
                      inp[2].events+wait_for))

    def update_z1(self, outp, inp, par=[], idx=0, idxq=0,
                  bound_cond=0, wait_for=[]):
        return self.prg[idx].update_z1(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX), None,
            outp.data, inp[0].data, inp[1].data,
            inp[2].data, inp[3].data, inp[4].data,
            np.float32(par[0]), np.float32(par[1]),
            np.float32(1/par[2]), np.int32(self.unknowns_TGV),
            np.int32(self.unknowns_H1), np.float32(1 / (1 + par[0] / par[3])),
            wait_for=(outp.events+inp[0].events+inp[1].events +
                      inp[2].events+inp[3].events+inp[4].events+wait_for))

    def update_z1_tv(self, outp, inp, par=[], idx=0, idxq=0,
                     bound_cond=0, wait_for=[]):
        return self.prg[idx].update_z1_tv(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX), None,
            outp.data, inp[0].data, inp[0].data, inp[0].data,
            np.float32(par[0]),
            np.float32(par[1]),
            np.float32(1/par[2]), np.int32(self.unknowns_TGV),
            np.int32(self.unknowns_H1), np.float32(1 / (1 + par[0] / par[3])),
            wait_for=(outp.events+inp[0].events +
                      inp[1].events+inp[2].events+wait_for))

    def update_z2(self, outp, inp, par=[], idx=0, idxq=0,
                  bound_cond=0, wait_for=[]):
        return self.prg[idx].update_z2(
            self.queue[4*idx+idxq],
            (self.overlap+self.par_slices, self.dimY, self.dimX), None,
            outp.data, inp[0].data, inp[1].data, inp[2].data,
            np.float32(par[0]),
            np.float32(par[1]),
            np.float32(1/par[2]),  np.int32(self.unknowns),
            wait_for=(outp.events+inp[0].events +
                      inp[1].events+inp[2].events+wait_for))

    def update_r(self, outp, inp, par=[], idx=0, idxq=0,
                 bound_cond=0, wait_for=[]):
        return self.prg[idx].update_r(
            self.queue[4*idx+idxq], (outp.size,), None,
            outp.data, inp[0].data,
            inp[1].data, inp[2].data, inp[3].data,
            np.float32(par[0]), np.float32(par[1]),
            np.float32(1/(1+par[0]/self.irgn_par["lambd"])),
            wait_for=(outp.events+inp[0].events +
                      inp[1].events+inp[2].events+wait_for))

    def update_v(self, outp, inp, par=[], idx=0, idxq=0,
                 bound_cond=0, wait_for=[]):
        return self.prg[idx].update_v(
            self.queue[4*idx+idxq], (outp[..., 0].size,), None,
            outp.data, inp[0].data, inp[1].data, np.float32(par[0]),
            wait_for=outp.events+inp[0].events+inp[1].events+wait_for)

    def eval_const(self):
        num_const = (len(self.model.constraints))
        min_const = np.zeros((num_const), dtype=np.float32)
        max_const = np.zeros((num_const), dtype=np.float32)
        real_const = np.zeros((num_const), dtype=np.int32)
        for j in range(num_const):
            min_const[j] = np.float32(self.model.constraints[j].min)
            max_const[j] = np.float32(self.model.constraints[j].max)
            real_const[j] = np.int32(self.model.constraints[j].real)

        self.min_const = []
        self.max_const = []
        self.real_const = []
        for j in range(self.num_dev):
            self.min_const.append(
                clarray.to_device(self.queue[4*j], min_const))
            self.max_const.append(
                clarray.to_device(self.queue[4*j], max_const))
            self.real_const.append(
                clarray.to_device(self.queue[4*j], real_const))

###############################################################################
# Scale before gradient #######################################################
###############################################################################
    def set_scale(self, inp):
        x = np.require(np.transpose(inp, [1, 0, 2, 3]), requirements='C')
        grad = np.zeros_like(self.z1)
        self.stream_grad.eval([grad], [[x]])
        grad = np.require(np.transpose(grad, [1, 0, 2, 3, 4]),
                          requirements='C')
        x = np.require(np.transpose(x, [1, 0, 2, 3]), requirements='C')
        scale = np.reshape(
            x, (self.unknowns, self.NSlice * self.dimY * self.dimX))
        grad = np.reshape(
            grad, (self.unknowns, self.NSlice * self.dimY * self.dimX * 4))
        print("Diff between x: ", np.linalg.norm(scale, axis=-1))
        print("Diff between grad x: ", np.linalg.norm(grad, axis=-1))
        scale = np.linalg.norm(grad, axis=-1)
        scale = 1/scale
        scale[~np.isfinite(scale)] = 1
        sum_scale = np.linalg.norm(
            scale[:self.unknowns_TGV])/(1000/np.sqrt(self.NSlice))
        for i in range(self.num_dev):
            for j in range(x.shape[0])[:self.unknowns_TGV]:
                self.ratio[i][j] = scale[j] / sum_scale
        sum_scale = np.linalg.norm(
            scale[self.unknowns_TGV:])/(1000)
        for i in range(self.num_dev):
            for j in range(x.shape[0])[self.unknowns_TGV:]:
                self.ratio[i][j] = scale[j] / sum_scale
        print("Ratio: ", self.ratio[0])

    def execute(self, TV=0, imagespace=0, reco_2D=0):
        if reco_2D:
            NotImplementedError("2D currently not implemented, "
                                "3D can be used with a single slice.")
        else:
            self.irgn_par["lambd"] *= self.SNR_est
            self.delta = self.irgn_par["delta"]
            self.delta_max = self.irgn_par["delta_max"]
            self.gamma = self.irgn_par["gamma"]
            self.omega = self.irgn_par["omega"]
            self._setup_reg_tmp_arrays(TV)
            self.execute_3D(TV)

###############################################################################
# Start a 3D Reconstruction, set TV to True to perform TV instead of TGV#######
# Precompute Model and Gradient values for xk #################################
# Call inner optimization #####################################################
# input: bool to switch between TV (1) and TGV (0) regularization #############
# output: optimal value of x ##################################################
###############################################################################
    def execute_3D(self, TV=0):

        iters = self.irgn_par["start_iters"]
        result = np.copy(self.model.guess)
        self.data = np.require(
            np.transpose(self.data, self.dat_trans_axes), requirements='C')

        for ign in range(self.irgn_par["max_gn_it"]):
            start = time.time()

            self.grad_x = np.nan_to_num(self.model.execute_gradient(result))

            self._balance_model_gradients(result, ign)
            self.set_scale(result)

            self.step_val = np.nan_to_num(self.model.execute_forward(result))
            self.step_val = np.require(
                np.transpose(self.step_val, [1, 0, 2, 3]), requirements='C')
            self.grad_x = np.require(
                np.transpose(self.grad_x, [2, 0, 1, 3, 4]), requirements='C')

            self._update_reg_par(result, ign)

            result = self.irgn_solve_3D(result, iters, ign, TV)

            iters = np.fmin(iters * 2, self.irgn_par["max_iters"])

            end = time.time() - start

            self.gn_res.append(self.fval)
            print("-" * 75)
            print("GN-Iter: %d  Elapsed time: %f seconds" % (ign, end))
            print("-" * 75)
            if np.abs(self.fval_old - self.fval) / self.fval_init < \
               self.irgn_par["tol"]:
                print("Terminated at GN-iteration %d because "
                      "the energy decrease was less than %.3e" %
                      (ign, np.abs(self.fval_old - self.fval) /
                       self.fval_init))
                self.calc_residual(
                    np.require(np.transpose(result, [1, 0, 2, 3]),
                               requirements='C'),
                    ign+1, TV)
                self.savetofile(ign, self.model.rescale(result), TV)
                break
            self.fval_old = self.fval
            self.savetofile(ign, self.model.rescale(result), TV)

        self.calc_residual(
            np.require(
                np.transpose(result, [1, 0, 2, 3]),
                requirements='C'),
            ign+1, TV)

    def _update_reg_par(self, result, ign):
        self.irgn_par["delta_max"] = (self.delta_max /
                                      1e3 * np.linalg.norm(result))
        self.irgn_par["delta"] = np.minimum(
            self.delta /
            (1e3)*np.linalg.norm(result)*self.irgn_par["delta_inc"]**ign,
            self.irgn_par["delta_max"])
        self.irgn_par["gamma"] = np.maximum(
            self.gamma * self.irgn_par["gamma_dec"]**ign,
            self.irgn_par["gamma_min"])
        self.irgn_par["omega"] = np.maximum(
            self.omega * self.irgn_par["omega_dec"]**ign,
            self.irgn_par["omega_min"])

    def _balance_model_gradients(self, result, ind):
        scale = np.reshape(
            self.grad_x,
            (self.unknowns,
             self.NScan * self.NSlice * self.dimY * self.dimX))
        scale = np.linalg.norm(scale, axis=-1)
        print("Initial norm of the model Gradient: \n", scale)
        scale = 1e3 / np.sqrt(self.unknowns) / scale
        print("Scalefactor of the model Gradient: \n", scale)
        if not np.mod(ind, 1):
            for uk in range(self.unknowns):
                self.model.constraints[uk].update(scale[uk])
                result[uk, ...] *= self.model.uk_scale[uk]
                self.grad_x[uk] /= self.model.uk_scale[uk]
                self.model.uk_scale[uk] *= scale[uk]
                result[uk, ...] /= self.model.uk_scale[uk]
                self.grad_x[uk] *= self.model.uk_scale[uk]
        scale = np.reshape(
            self.grad_x,
            (self.unknowns,
             self.NScan * self.NSlice * self.dimY * self.dimX))
        scale = np.linalg.norm(scale, axis=-1)
        print("Scale of the model Gradient: \n", scale)

    def _setup_reg_tmp_arrays(self, TV):
        if TV == 1:
            self.tau = np.float32(1/np.sqrt(8))
            self.beta_line = 400
            self.theta_line = np.float32(1.0)
        elif TV == 0:
            L = np.float32(0.5*(18.0 + np.sqrt(33)))
            self.tau = np.float32(1/np.sqrt(L))
            self.beta_line = 400
            self.theta_line = np.float32(1.0)
            self.v = np.zeros(
                ([self.NSlice, self.unknowns, self.dimY, self.dimX, 4]),
                dtype=DTYPE)
            self.z2 = np.zeros(
                ([self.NSlice, self.unknowns, self.dimY, self.dimX, 8]),
                dtype=DTYPE)
        else:
            raise NotImplementedError("Not implemented")
        self._setupstreamingops(TV)

        self.r = np.zeros_like(self.data, dtype=DTYPE)
        self.r = np.require(np.transpose(self.r, self.dat_trans_axes),
                            requirements='C')
        self.z1 = np.zeros(
            ([self.NSlice, self.unknowns, self.dimY, self.dimX, 4]),
            dtype=DTYPE)

###############################################################################
# New .hdf5 save files ########################################################
###############################################################################
    def savetofile(self, myit, result, TV):
        f = h5py.File(self.par["outdir"]+"output_" + self.par["fname"], "a")
        if not TV:
            f.create_dataset("tgv_result_iter_"+str(myit), result.shape,
                             dtype=DTYPE, data=result)
            f.attrs['res_tgv_iter_'+str(myit)] = self.fval
        else:
            f.create_dataset("tv_result_"+str(myit), result.shape,
                             dtype=DTYPE, data=result)
            f.attrs['res_tv_iter_'+str(myit)] = self.fval
        f.close()

###############################################################################
# Precompute constant terms of the GN linearization step ######################
# input: linearization point x ################################################
# numeber of innner iterations iters ##########################################
# Data ########################################################################
# bool to switch between TV (1) and TGV (0) regularization ####################
# output: optimal value of x for the inner GN step ############################
###############################################################################
###############################################################################
    def irgn_solve_3D_kspace(self, x, iters, GN_it, TV=0):
        x = np.require(np.transpose(x, [1, 0, 2, 3]), requirements='C')
        b = np.zeros(self.data_shape, dtype=DTYPE)
        DGk = np.zeros(self.data_shape, dtype=DTYPE)

        self.op.FTstr.eval(
            [b],
            [[self.step_val[:, :, None, ...]*self.C[:, None, ...]]])

        self.op.fwd(
            [DGk],
            [[x, self.C, self.grad_x]])
        res = self.data - b + DGk

        self.calc_residual_kspace(x, GN_it, TV)

        if TV == 1:
            x = self.tv_solve_3D(x, res, iters)
        elif TV == 0:
            x = self.tgv_solve_3D(x, res, iters)
        x = np.require(np.transpose(x, [1, 0, 2, 3]), requirements='C')
        return x

    def irgn_solve_3D_kspaceSMS(self, x, iters, GN_it, TV=0):

        b = np.zeros(self.data_shape_T, dtype=DTYPE)

        self.C = np.require(
            np.transpose(
                self.C,
                (1, 0, 2, 3)),
            requirements='C')
        self.step_val = np.require(
            np.transpose(
                self.step_val,
                (1, 0, 2, 3)),
            requirements='C')
        self.op.FTstr.eval(
            [b],
            [[self.step_val[:, None, ...]*self.C[None, ...]]])

        self.C = np.require(
            np.transpose(
                self.C,
                (1, 0, 2, 3)),
            requirements='C')
        self.step_val = np.require(
            np.transpose(
                self.step_val,
                (1, 0, 2, 3)),
            requirements='C')

        x = np.require(np.transpose(x, [1, 0, 2, 3]), requirements='C')
        self.calc_residual(x, GN_it, TV)

        DGk = self.op.fwdoop([[x, self.C, self.grad_x]])
        b = np.require(
            np.transpose(
                b,
                self.dat_trans_axes),
            requirements='C')
        res = self.data - b + DGk

        if TV == 1:
            x = self.tv_solve_3D(x, res, iters)
        elif TV == 0:
            x = self.tgv_solve_3D(x, res, iters)
        x = np.require(np.transpose(x, [1, 0, 2, 3]), requirements='C')
        return x

    def irgn_solve_3D_imagespace(self, x, iters, GN_it, TV=0):

        x = np.require(np.transpose(x, [1, 0, 2, 3]), requirements='C')
        DGk = np.zeros(self.data_shape, DTYPE)

        self.op.fwd(
            [DGk],
            [[x, self.C, self.grad_x]])

        res = self.data - self.step_val + DGk

        self.calc_residual_imagespace(x, GN_it, TV)

        if TV == 1:
            x = self.tv_solve_3D(x, res, iters)
        elif TV == 0:
            x = self.tgv_solve_3D(x, res, iters)
        x = np.require(np.transpose(x, [1, 0, 2, 3]), requirements='C')
        return x

    def calc_residual_kspace(self, x, GN_it, TV=0):
        b = np.zeros(self.data_shape, dtype=DTYPE)
        grad = np.zeros_like(self.z1)
        self.stream_grad.eval([grad], [[x]])
        self.op.FTstr.eval(
            [b],
            [[self.step_val[:, :, None, ...]*self.C[:, None, ...]]])
        if TV == 1:
            self.fval = (
                self.irgn_par["lambd"]/2*np.linalg.norm(self.data - b)**2 +
                self.irgn_par["gamma"]*np.sum(np.abs(
                    grad[:, :self.unknowns_TGV])) +
                self.irgn_par["omega"] / 2 *
                np.linalg.norm(grad[:, self.unknowns_TGV:])**2)
        elif TV == 0:
            sym_grad = np.zeros_like(self.z2)
            self.sym_grad_streamed.eval([sym_grad], [[self.v]])
            self.fval = (
                self.irgn_par["lambd"]/2*np.linalg.norm(self.data - b)**2 +
                self.irgn_par["gamma"]*np.sum(np.abs(
                    grad[:, :self.unknowns_TGV]-self.v)) +
                self.irgn_par["gamma"]*(2)*np.sum(np.abs(sym_grad)) +
                self.irgn_par["omega"] / 2 *
                np.linalg.norm(grad[:, self.unknowns_TGV:])**2)
            del sym_grad
        del grad, b

        if GN_it == 0:
            self.fval_init = self.fval
        print("-" * 75)
        print("Function value at GN-Step %i: %f" %
              (GN_it, 1e3*self.fval / self.fval_init))
        print("-" * 75)

    def calc_residual_imagespace(self, x, GN_it, TV=0):
        grad = np.zeros_like(self.z1)
        self.stream_grad.eval([grad], [[x]])
        if TV == 1:
            self.fval = (
                self.irgn_par["lambd"]/2 *
                np.linalg.norm(self.data - self.step_val)**2 +
                self.irgn_par["gamma"]*np.sum(
                    np.abs(grad[:, :self.unknowns_TGV])) +
                self.irgn_par["omega"] / 2 *
                np.linalg.norm(grad[:, self.unknowns_TGV:])**2)
        elif TV == 0:
            sym_grad = np.zeros_like(self.z2)
            self.sym_grad_streamed.eval([sym_grad], [[self.v]])
            self.fval = (
                self.irgn_par["lambd"]/2 *
                np.linalg.norm(self.data - self.step_val)**2 +
                self.irgn_par["gamma"]*np.sum(
                    np.abs(grad[:, :self.unknowns_TGV]-self.v)) +
                self.irgn_par["gamma"]*(2)*np.sum(np.abs(sym_grad)) +
                self.irgn_par["omega"] / 2 *
                np.linalg.norm(grad[:, self.unknowns_TGV:])**2)
            del sym_grad
        del grad
        if GN_it == 0:
            self.fval_init = self.fval
        print("-" * 75)
        print("Function value at GN-Step %i: %f" %
              (GN_it, 1e3*self.fval / self.fval_init))
        print("-" * 75)

    def calc_residual_kspaceSMS(self, x, GN_it, TV=0):
        self.C = np.require(
            np.transpose(
                self.C,
                (1, 0, 2, 3)),
            requirements='C')
        self.step_val = np.require(
            np.transpose(
                self.step_val,
                (1, 0, 2, 3)),
            requirements='C')
        b = np.zeros(self.data_shape_T, dtype=DTYPE)
        grad = np.zeros_like(self.z1)
        self.stream_grad.eval([grad], [[x]])
        self.op.FTstr.eval(
            [b],
            [[self.step_val[:, None, ...]*self.C[None, ...]]])
        b = np.require(
            np.transpose(
                b,
                self.dat_trans_axes),
            requirements='C')
        if TV == 1:
            self.fval = (
                self.irgn_par["lambd"]/2*np.linalg.norm(self.data - b)**2 +
                self.irgn_par["gamma"]*np.sum(np.abs(
                    grad[:, :self.unknowns_TGV])) +
                self.irgn_par["omega"] / 2 *
                np.linalg.norm(grad[:, self.unknowns_TGV:])**2)
        elif TV == 0:
            sym_grad = np.zeros_like(self.z2)
            self.sym_grad_streamed.eval([sym_grad], [[self.v]])
            self.fval = (
                self.irgn_par["lambd"]/2*np.linalg.norm(self.data - b)**2 +
                self.irgn_par["gamma"]*np.sum(np.abs(
                    grad[:, :self.unknowns_TGV]-self.v)) +
                self.irgn_par["gamma"]*(2)*np.sum(np.abs(sym_grad)) +
                self.irgn_par["omega"] / 2 *
                np.linalg.norm(grad[:, self.unknowns_TGV:])**2)

        if GN_it == 0:
            self.fval_init = self.fval
        print("-" * 75)
        print("Function value at GN-Step %i: %f" %
              (GN_it, 1e3*self.fval / self.fval_init))
        print("-" * 75)
        self.C = np.require(
            np.transpose(
                self.C,
                (1, 0, 2, 3)),
            requirements='C')
        self.step_val = np.require(
            np.transpose(
                self.step_val,
                (1, 0, 2, 3)),
            requirements='C')

    def tgv_solve_3D(self, x, res, iters):
        alpha = self.irgn_par["gamma"]
        beta = self.irgn_par["gamma"] * 2

        tau = self.tau
        tau_new = np.float32(0)

        xk = x.copy()
        x_new = np.zeros_like(x)

        r = np.zeros_like(self.r)
        r_new = np.zeros_like(r)
        z1 = np.zeros_like(self.z1)
        z1_new = np.zeros_like(z1)
        z2 = np.zeros_like(self.z2)
        z2_new = np.zeros_like(z2)
        v = np.zeros_like(self.v)
        v_new = np.zeros_like(v)
        res = (res).astype(DTYPE)

        delta = self.irgn_par["delta"]
        omega = self.irgn_par["omega"]
        mu = 1/delta

        theta_line = self.theta_line
        beta_line = self.beta_line
        beta_new = np.float32(0)
        mu_line = np.float32(0.5)
        delta_line = np.float32(1)
        ynorm1 = np.float32(0.0)
        lhs1 = np.float32(0.0)
        ynorm2 = np.float32(0.0)
        lhs2 = np.float32(0.0)
        primal = np.float32(0.0)
        primal_new = np.float32(0)
        dual = np.float32(0.0)
        gap_init = np.float32(0.0)
        gap_old = np.float32(0.0)
        gap = np.float32(0.0)
        self.eval_const()

        Kyk1 = np.zeros_like(x)
        Kyk1_new = np.zeros_like(x)
        Kyk2 = np.zeros_like(z1)
        Kyk2_new = np.zeros_like(z1)
        gradx = np.zeros_like(z1)
        gradx_xold = np.zeros_like(z1)
        symgrad_v = np.zeros_like(z2)
        symgrad_v_vold = np.zeros_like(z2)
        Axold = np.zeros_like(res)
        Ax = np.zeros_like(res)

        # Warmup
        self.stream_initial_1.eval(
            [Axold, Kyk1, symgrad_v_vold],
            [[x, self.C, self.grad_x], [r, z1, self.C, self.grad_x, []], [v]],
            [self.ratio])
        self.stream_initial_2.eval(
            [gradx_xold, Kyk2],
            [[x], [z2, z1, []]])
        # Start Iterations
        for myit in range(iters):

            self.update_primal_1.eval(
                [x_new, gradx, Ax],
                [[x, Kyk1, xk], [], [[], self.C, self.grad_x]],
                [tau, delta])
            self.update_primal_2.eval(
                [v_new, symgrad_v],
                [[v, Kyk2], []],
                [tau])

            beta_new = beta_line*(1+mu*tau)
            tau_new = tau*np.sqrt(beta_line/beta_new*(1+theta_line))
            beta_line = beta_new

            while True:
                theta_line = tau_new/tau
                (lhs1, ynorm1) = self.update_dual_1.evalwithnorm(
                    [z1_new, r_new, Kyk1_new],
                    [[z1, gradx, gradx_xold, v_new, v],
                     [r, Ax, Axold, res],
                     [[], [], self.C, self.grad_x, Kyk1]],
                    [beta_line*tau_new, theta_line,
                     alpha, omega, self.ratio])
                (lhs2, ynorm2) = self.update_dual_2.evalwithnorm(
                    [z2_new, Kyk2_new],
                    [[z2, symgrad_v, symgrad_v_vold], [[], z1_new, Kyk2]],
                    [beta_line*tau_new, theta_line, beta])

                if np.sqrt(beta_line)*tau_new*(abs(lhs1+lhs2)**(1/2)) <= \
                   (abs(ynorm1+ynorm2)**(1/2))*delta_line:
                    break
                else:
                    tau_new = tau_new*mu_line

            (Kyk1, Kyk1_new, Kyk2, Kyk2_new, Axold, Ax, z1, z1_new,
             z2, z2_new, r, r_new, gradx_xold, gradx, symgrad_v_vold,
             symgrad_v, tau) = (
             Kyk1_new, Kyk1, Kyk2_new, Kyk2, Ax, Axold, z1_new, z1,
             z2_new, z2, r_new, r, gradx, gradx_xold, symgrad_v,
             symgrad_v_vold, tau_new)

            if not np.mod(myit, 10):
                if self.irgn_par["display_iterations"]:
                    self.model.plot_unknowns(
                        np.transpose(x_new, [1, 0, 2, 3]))
                if self.unknowns_H1 > 0:
                    primal_new = (
                        self.irgn_par["lambd"]/2 *
                        np.vdot(Axold-res, Axold-res) +
                        alpha*np.sum(abs((gradx[:, :self.unknowns_TGV]-v))) +
                        beta*np.sum(abs(symgrad_v)) +
                        1/(2*delta)*np.vdot(x_new-xk, x_new-xk) +
                        self.irgn_par["omega"] / 2 *
                        np.vdot(gradx[:, :self.unknowns_TGV],
                                gradx[:, :self.unknowns_TGV])).real

                    dual = (
                        - delta/2*np.vdot(-Kyk1.flatten(), -Kyk1.flatten())
                        - np.vdot(xk.flatten(), (-Kyk1).flatten())
                        + np.sum(Kyk2)
                        - 1/(2*self.irgn_par["lambd"])
                        * np.vdot(r.flatten(), r.flatten())
                        - np.vdot(res.flatten(), r.flatten())
                        - 1 / (2 * self.irgn_par["omega"])
                        * np.vdot(z1[:, :self.unknowns_TGV],
                                  z1[:, :self.unknowns_TGV])).real
                else:
                    primal_new = (
                        self.irgn_par["lambd"]/2 *
                        np.vdot(Axold-res, Axold-res) +
                        alpha*np.sum(abs((gradx[:, :self.unknowns_TGV]-v))) +
                        beta*np.sum(abs(symgrad_v)) +
                        1/(2*delta)*np.vdot(x_new-xk, x_new-xk)).real

                    dual = (
                        - delta/2*np.vdot(-Kyk1.flatten(), -Kyk1.flatten())
                        - np.vdot(xk.flatten(), (-Kyk1).flatten())
                        + np.sum(Kyk2)
                        - 1/(2*self.irgn_par["lambd"])
                        * np.vdot(r.flatten(), r.flatten())
                        - np.vdot(res.flatten(), r.flatten())).real

                gap = np.abs(primal_new - dual)
                if myit == 0:
                    gap_init = gap
                if np.abs((primal-primal_new) / self.fval_init) <\
                   self.irgn_par["tol"]:
                    print("Terminated at iteration %d because the energy "
                          "decrease in the primal problem was less than %.3e" %
                          (myit, np.abs(primal-primal_new) / self.fval_init))
                    self.v = v_new
                    self.r = r
                    self.z1 = z1
                    self.z2 = z2
                    return x_new
                if (gap > gap_old*self.irgn_par["stag"]) and myit > 1:
                    self.v = v_new
                    self.r = r
                    self.z1 = z1
                    self.z2 = z2
                    print("Terminated at iteration %d "
                          "because the method stagnated" % (myit))
                    return x_new
                if np.abs((gap-gap_old)/gap_init) < self.irgn_par["tol"]:
                    self.v = v_new
                    self.r = r
                    self.z1 = z1
                    self.z2 = z2
                    print("Terminated at iteration %d because the relative "
                          "energy decrease of the PD gap was less than %.3e" %
                          (myit, np.abs((gap-gap_old) / gap_init)))
                    return x_new
                primal = primal_new
                gap_old = gap
                sys.stdout.write(
                    "Iteration: %04d ---- Primal: "
                    "%2.2e, Dual: %2.2e, Gap: %2.2e \r"
                    % (myit, 1000*primal/self.fval_init,
                       1000*dual/self.fval_init,
                       1000*gap/self.fval_init))
                sys.stdout.flush()
            (x, x_new) = (x_new, x)
            (v, v_new) = (v_new, v)

        self.v = v
        self.r = r
        self.z1 = z1
        self.z2 = z2
        return x

    def tgv_solve_3DSMS(self, x, res, iters):
        alpha = self.irgn_par["gamma"]
        beta = self.irgn_par["gamma"] * 2

        tau = self.tau
        tau_new = np.float32(0)

        xk = x.copy()
        x_new = np.zeros_like(x)

        r = np.zeros_like(self.r)
        r_new = np.zeros_like(r)
        z1 = np.zeros_like(self.z1)
        z1_new = np.zeros_like(z1)
        z2 = np.zeros_like(self.z2)
        z2_new = np.zeros_like(z2)
        v = np.zeros_like(self.v)
        v_new = np.zeros_like(v)
        res = (res).astype(DTYPE)

        delta = self.irgn_par["delta"]
        omega = self.irgn_par["omega"]
        mu = 1/delta

        theta_line = self.theta_line
        beta_line = self.beta_line
        beta_new = np.float32(0)
        mu_line = np.float32(0.5)
        delta_line = np.float32(1)
        ynorm1 = np.float32(0.0)
        lhs1 = np.float32(0.0)
        ynorm2 = np.float32(0.0)
        lhs2 = np.float32(0.0)
        ynorm3 = np.float32(0.0)
        lhs3 = np.float32(0.0)
        ynorm4 = np.float32(0.0)
        lhs4 = np.float32(0.0)
        primal = np.float32(0.0)
        primal_new = np.float32(0)
        dual = np.float32(0.0)
        gap_init = np.float32(0.0)
        gap_old = np.float32(0.0)
        gap = np.float32(0.0)
        self.eval_const()

        Kyk1 = np.zeros_like(x)
        Kyk1_new = np.zeros_like(x)
        Kyk2 = np.zeros_like(z1)
        Kyk2_new = np.zeros_like(z1)
        gradx = np.zeros_like(z1)
        gradx_xold = np.zeros_like(z1)
        symgrad_v = np.zeros_like(z2)
        symgrad_v_vold = np.zeros_like(z2)
        Axold = np.zeros_like(res)
        Ax = np.zeros_like(res)

        # Warmup
        Axold = self.op.fwdoop(
            [[x, self.C, self.grad_x]])
        self.op.adj(
            [Kyk1],
            [[r, z1, self.C, self.grad_x, []]], [self.ratio])
        self.sym_grad_streamed.eval(
            [symgrad_v_vold],
            [[v]])

        self.stream_initial_2.eval(
            [gradx_xold, Kyk2],
            [[x], [z2, z1, []]])
        # Start Iterations
        for myit in range(iters):
            self.update_primal_1.eval(
                [x_new, gradx],
                [[x, Kyk1, xk], []],
                [tau, delta])
            Ax = self.op.fwdoop(
                [[x_new, self.C, self.grad_x]])

            self.update_primal_2.eval(
                [v_new, symgrad_v],
                [[v, Kyk2], []],
                [tau])
            beta_new = beta_line*(1+mu*tau)
            tau_new = tau*np.sqrt(beta_line/beta_new*(1+theta_line))
            beta_line = beta_new

            while True:
                theta_line = tau_new/tau

                (lhs1, ynorm1) = self.stream_update_z1.evalwithnorm(
                    [z1_new],
                    [[z1, gradx, gradx_xold, v_new, v]],
                    [beta_line*tau_new, theta_line,
                     alpha, omega])
                (lhs2, ynorm2) = self.stream_update_r.evalwithnorm(
                    [r_new],
                    [[r, Ax, Axold, res]],
                    [beta_line*tau_new, theta_line,
                     alpha, omega])
                (lhs3, ynorm3) = self.op.adjKyk1(
                    [Kyk1_new],
                    [[r_new, z1_new, self.C, self.grad_x, Kyk1]], [self.ratio])

                (lhs4, ynorm4) = self.update_dual_2.evalwithnorm(
                    [z2_new, Kyk2_new],
                    [[z2, symgrad_v, symgrad_v_vold], [[], z1_new, Kyk2]],
                    [beta_line*tau_new, theta_line, beta])

                if np.sqrt(beta_line)*tau_new*(
                    abs(lhs1+lhs2+lhs3+lhs4)**(1/2)) <= \
                   (abs(ynorm1+ynorm2+ynorm3+ynorm4)**(1/2))*delta_line:
                    break
                else:
                    tau_new = tau_new*mu_line

            (Kyk1, Kyk1_new, Kyk2, Kyk2_new, Axold, Ax, z1, z1_new,
             z2, z2_new, r, r_new, gradx_xold, gradx, symgrad_v_vold,
             symgrad_v, tau) = (
             Kyk1_new, Kyk1, Kyk2_new, Kyk2, Ax, Axold, z1_new, z1,
             z2_new, z2, r_new, r, gradx, gradx_xold, symgrad_v,
             symgrad_v_vold, tau_new)

            if not np.mod(myit, 10):
                if self.irgn_par["display_iterations"]:
                    self.model.plot_unknowns(
                        np.transpose(x_new, [1, 0, 2, 3]))
                if self.unknowns_H1 > 0:
                    primal_new = (
                        self.irgn_par["lambd"]/2 *
                        np.vdot(Axold-res, Axold-res) +
                        alpha*np.sum(abs((gradx[:, :self.unknowns_TGV]-v))) +
                        beta*np.sum(abs(symgrad_v)) +
                        1/(2*delta)*np.vdot(x_new-xk, x_new-xk) +
                        self.irgn_par["omega"] / 2 *
                        np.vdot(gradx[:, :self.unknowns_TGV],
                                gradx[:, :self.unknowns_TGV])).real

                    dual = (
                        - delta/2*np.vdot(-Kyk1.flatten(), -Kyk1.flatten())
                        - np.vdot(xk.flatten(), (-Kyk1).flatten())
                        + np.sum(Kyk2)
                        - 1/(2*self.irgn_par["lambd"])
                        * np.vdot(r.flatten(), r.flatten())
                        - np.vdot(res.flatten(), r.flatten())
                        - 1 / (2 * self.irgn_par["omega"])
                        * np.vdot(z1[:, :self.unknowns_TGV],
                                  z1[:, :self.unknowns_TGV])).real
                else:
                    primal_new = (
                        self.irgn_par["lambd"]/2 *
                        np.vdot(Axold-res, Axold-res) +
                        alpha*np.sum(abs((gradx-v))) +
                        beta*np.sum(abs(symgrad_v)) +
                        1/(2*delta)*np.vdot(x_new-xk, x_new-xk)).real

                    dual = (
                        - delta/2*np.vdot(-Kyk1.flatten(), -Kyk1.flatten())
                        - np.vdot(xk.flatten(), (-Kyk1).flatten())
                        + np.sum(Kyk2)
                        - 1/(2*self.irgn_par["lambd"])
                        * np.vdot(r.flatten(), r.flatten())
                        - np.vdot(res.flatten(), r.flatten())).real

                gap = np.abs(primal_new - dual)
                if myit == 0:
                    gap_init = gap
                if np.abs((primal-primal_new) / self.fval_init) <\
                   self.irgn_par["tol"]:
                    print("Terminated at iteration %d because the energy "
                          "decrease in the primal problem was less than %.3e" %
                          (myit, np.abs(primal-primal_new) / self.fval_init))
                    self.v = v_new
                    self.r = r
                    self.z1 = z1
                    self.z2 = z2
                    return x_new
                if (gap > gap_old*self.irgn_par["stag"]) and myit > 1:
                    self.v = v_new
                    self.r = r
                    self.z1 = z1
                    self.z2 = z2
                    print("Terminated at iteration %d "
                          "because the method stagnated" % (myit))
                    return x_new
                if np.abs((gap-gap_old)/gap_init) < self.irgn_par["tol"]:
                    self.v = v_new
                    self.r = r
                    self.z1 = z1
                    self.z2 = z2
                    print("Terminated at iteration %d because the relative "
                          "energy decrease of the PD gap was less than %.3e" %
                          (myit, np.abs((gap-gap_old) / gap_init)))
                    return x_new
                primal = primal_new
                gap_old = gap
                sys.stdout.write(
                    "Iteration: %04d ---- Primal: "
                    "%2.2e, Dual: %2.2e, Gap: %2.2e \r"
                    % (myit, 1000*primal/self.fval_init,
                       1000*dual/self.fval_init,
                       1000*gap/self.fval_init))
                sys.stdout.flush()
            (x, x_new) = (x_new, x)
            (v, v_new) = (v_new, v)

        self.v = v
        self.r = r
        self.z1 = z1
        self.z2 = z2
        return x

    def tv_solve_3D(self, x, res, iters):
        alpha = self.irgn_par["gamma"]
        tau = self.tau
        tau_new = np.float32(0)

        xk = x.copy()
        x_new = np.zeros_like(x)

        r = np.zeros_like(self.r)
        r_new = np.zeros_like(r)
        z1 = np.zeros_like(self.z1)
        z1_new = np.zeros_like(z1)
        res = (res).astype(DTYPE)

        delta = self.irgn_par["delta"]
        omega = self.irgn_par["omega"]
        mu = 1/delta
        theta_line = np.float32(1.0)
        beta_line = np.float32(400)
        beta_new = np.float32(0)
        mu_line = np.float32(0.5)
        delta_line = np.float32(1)
        ynorm1 = np.float32(0.0)
        lhs1 = np.float32(0.0)
        ynorm2 = np.float32(0.0)
        lhs2 = np.float32(0.0)
        primal = np.float32(0.0)
        primal_new = np.float32(0)
        dual = np.float32(0.0)
        gap_init = np.float32(0.0)
        gap_old = np.float32(0.0)

        self.eval_const()

        Kyk1 = np.zeros_like(x)
        Kyk1_new = np.zeros_like(x)
        gradx = np.zeros_like(z1)
        gradx_xold = np.zeros_like(z1)
        Axold = np.zeros_like(res)
        Ax = np.zeros_like(res)

        # Warmup
        self.stream_initial_1.eval(
            [Axold, Kyk1],
            [[x, self.C, self.grad_x], [r, z1, self.C, self.grad_x, []]])
        self.stream_initial_2.eval(
            [gradx_xold],
            [[x]])

        for myit in range(iters):
            self.update_primal_1.eval(
                [x_new, gradx, Ax],
                [[x, Kyk1, xk], [], [[], self.C, self.grad_x]],
                [tau, delta])

            beta_new = beta_line*(1+mu*tau)
            tau_new = tau*np.sqrt(beta_line/beta_new*(1+theta_line))
            beta_line = beta_new

            while True:
                theta_line = tau_new/tau

                (lhs1, ynorm1) = self.update_dual_1.evalwithnorm(
                    [z1_new, r_new, Kyk1_new],
                    [[z1, gradx, gradx_xold],
                     [r, Ax, Axold, res],
                     [[], [], self.C, self.grad_x, Kyk1]],
                    [beta_line*tau_new, theta_line,
                     alpha, omega])

                if np.sqrt(beta_line)*tau_new*(abs(lhs1+lhs2)**(1/2)) <= \
                   (abs(ynorm1+ynorm2)**(1/2))*delta_line:
                    break
                else:
                    tau_new = tau_new*mu_line

            (Kyk1, Kyk1_new,  Axold, Ax, z1, z1_new, r, r_new, gradx_xold,
             gradx, tau) = (
             Kyk1_new, Kyk1,  Ax, Axold, z1_new, z1, r_new, r, gradx,
             gradx_xold, tau_new)

            if not np.mod(myit, 10):
                if self.irgn_par["display_iterations"]:
                    self.model.plot_unknowns(np.transpose(x_new, [1, 0, 2, 3]))
                if self.unknowns_H1 > 0:
                    primal_new = (
                        self.irgn_par["lambd"]/2 *
                        np.vdot(Axold-res, Axold-res) +
                        alpha*np.sum(abs((gradx[:, :self.unknowns_TGV]))) +
                        1/(2*delta)*np.vdot(x_new-xk, x_new-xk) +
                        self.irgn_par["omega"] / 2 *
                        np.vdot(gradx[:, :self.unknowns_TGV],
                                gradx[:, :self.unknowns_TGV])).real

                    dual = (
                        -delta/2*np.vdot(-Kyk1, -Kyk1) - np.vdot(xk, (-Kyk1))
                        - 1/(2*self.irgn_par["lambd"])*np.vdot(r, r)
                        - np.vdot(res, r)
                        - 1 / (2 * self.irgn_par["omega"])
                        * np.vdot(z1[:, :self.unknowns_TGV],
                                  z1[:, :self.unknowns_TGV])).real
                else:
                    primal_new = (
                        self.irgn_par["lambd"]/2 *
                        np.vdot(Axold-res, Axold-res) +
                        alpha*np.sum(abs((gradx[:, :self.unknowns_TGV]))) +
                        1/(2*delta)*np.vdot(x_new-xk, x_new-xk)).real

                    dual = (
                        -delta/2*np.vdot(-Kyk1, -Kyk1) - np.vdot(xk, (-Kyk1))
                        - 1/(2*self.irgn_par["lambd"])*np.vdot(r, r)
                        - np.vdot(res, r)).real

                gap = np.abs(primal_new - dual)
                if myit == 0:
                    gap_init = gap
                if np.abs(primal-primal_new) / self.fval_init < \
                   self.irgn_par["tol"]:
                    print("Terminated at iteration %d because the energy "
                          "decrease in the primal problem was less than %.3e" %
                          (myit, np.abs(primal-primal_new) / self.fval_init))
                    self.r = r
                    self.z1 = z1
                    return x_new
                if (gap > gap_old*self.irgn_par["stag"]) and myit > 1:
                    self.r = r
                    self.z1 = z1
                    print("Terminated at iteration %d because "
                          "the method stagnated" % (myit))
                    return x_new
                if np.abs((gap-gap_old)/gap_init) < self.irgn_par["tol"]:
                    self.r = r
                    self.z1 = z1
                    print("Terminated at iteration %d because the relative "
                          "energy decrease of the PD gap was less than %.3e" %
                          (myit, np.abs((gap-gap_old) / gap_init)))
                    return x_new
                primal = primal_new
                gap_old = gap
                sys.stdout.write(
                    "Iteration: %04d ---- "
                    "Primal: %2.2e, Dual: %2.2e, Gap: %2.2e \r"
                    % (myit, 1000 * primal / self.fval_init,
                       1000 * dual / self.fval_init,
                       1000 * gap / self.fval_init))
                sys.stdout.flush()
            (x, x_new) = (x_new, x)

        self.r = r
        self.z1 = z1
        return x

    def _setupstreamingops(self, TV):
        if not TV:
            symgrad_shape = self.unknown_shape + (8,)

        if not TV:
            self.sym_grad_streamed = self._defineoperator(
                [self.sym_grad],
                [symgrad_shape],
                [[self.grad_shape]])

        self.stream_initial_1 = self._defineoperator(
            [],
            [],
            [[]],
            reverse_dir=True)
        self.stream_initial_1 += self.op.fwdstr
        self.stream_initial_1 += self.op.adjstr
        if not TV:
            self.stream_initial_1 += self.sym_grad_streamed

        self.stream_grad = self._defineoperator(
            [self.f_grad],
            [self.grad_shape],
            [[self.unknown_shape]])
        if not TV:
            self.stream_Kyk2 = self._defineoperator(
                [self.update_Kyk2],
                [self.grad_shape],
                [[symgrad_shape,
                  self.grad_shape,
                  self.grad_shape]])

            self.stream_initial_2 = self._defineoperator(
                [],
                [],
                [[]])

            self.stream_initial_2 += self.stream_grad
            self.stream_initial_2 += self.stream_Kyk2
        self.stream_primal = self._defineoperator(
            [self.update_primal],
            [self.unknown_shape],
            [[self.unknown_shape,
              self.unknown_shape,
              self.unknown_shape]])

        self.update_primal_1 = self._defineoperator(
            [],
            [],
            [[]])

        self.update_primal_1 += self.stream_primal
        self.update_primal_1 += self.stream_grad
        self.update_primal_1 += self.op.fwdstr

        self.update_primal_1.connectouttoin(0, (1, 0))
        self.update_primal_1.connectouttoin(0, (2, 0))

        if not TV:
            self.stream_update_v = self._defineoperator(
                [self.update_v],
                [self.grad_shape],
                [[self.grad_shape,
                  self.grad_shape]])

            self.update_primal_2 = self._defineoperator(
                [],
                [],
                [[]],
                reverse_dir=True)

            self.update_primal_2 += self.stream_update_v
            self.update_primal_2 += self.sym_grad_streamed
            self.update_primal_2.connectouttoin(0, (1, 0))

        if TV:
            self.stream_update_z1 = self._defineoperator(
                [self.update_z1],
                [self.grad_shape],
                [[self.grad_shape,
                  self.grad_shape,
                  self.grad_shape]])
        else:
            self.stream_update_z1 = self._defineoperator(
                [self.update_z1],
                [self.grad_shape],
                [[self.grad_shape,
                 self. grad_shape,
                 self. grad_shape,
                 self. grad_shape,
                 self. grad_shape]])

        self.stream_update_r = self._defineoperator(
            [self.update_r],
            [self.data_shape],
            [[self.data_shape,
              self.data_shape,
              self.data_shape,
              self.data_shape]])

        self.update_dual_1 = self._defineoperator(
            [],
            [],
            [[]],
            reverse_dir=True,
            posofnorm=[False, False, True])

        self.update_dual_1 += self.stream_update_z1
        self.update_dual_1 += self.stream_update_r
        self.update_dual_1 += self.op.adjstr
        self.update_dual_1.connectouttoin(0, (2, 1))
        self.update_dual_1.connectouttoin(1, (2, 0))

        del self.stream_update_z1, self.stream_update_r, \
            self.stream_update_v, self.stream_primal

        if not TV:
            self.stream_update_z2 = self._defineoperator(
                [self.update_z2],
                [symgrad_shape],
                [[symgrad_shape,
                  symgrad_shape,
                  symgrad_shape]])

            self.update_dual_2 = self._defineoperator(
                [],
                [],
                [[]],
                posofnorm=[False, True])

            self.update_dual_2 += self.stream_update_z2
            self.update_dual_2 += self.stream_Kyk2
            self.update_dual_2.connectouttoin(0, (1, 0))
            del self.stream_Kyk2, self.stream_update_z2

    def _defineoperator(self,
                        functions,
                        outp,
                        inp,
                        reverse_dir=False,
                        posofnorm=[],
                        slices=None):
        if slices is None:
            slices = self.NSlice
        return streaming.stream(
            functions,
            outp,
            inp,
            self.par_slices,
            self.overlap,
            slices,
            self.queue,
            self.num_dev,
            reverse_dir,
            posofnorm)

    def _setupstreamingopsSMS(self, TV):

        if not TV:
            symgrad_shape = self.unknown_shape + (8,)

        if not TV:
            self.sym_grad_streamed = self._defineoperator(
                [self.sym_grad],
                [symgrad_shape],
                [[self.grad_shape]])

        self.stream_grad = self._defineoperator(
            [self.f_grad],
            [self.grad_shape],
            [[self.unknown_shape]])
        if not TV:
            self.stream_Kyk2 = self._defineoperator(
                [self.update_Kyk2],
                [self.grad_shape],
                [[symgrad_shape,
                  self.grad_shape,
                  self.grad_shape]])

            self.stream_initial_2 = self._defineoperator(
                [],
                [],
                [[]])

            self.stream_initial_2 += self.stream_grad
            self.stream_initial_2 += self.stream_Kyk2

        self.stream_primal = self._defineoperator(
            [self.update_primal],
            [self.unknown_shape],
            [[self.unknown_shape,
              self.unknown_shape,
              self.unknown_shape]])

        self.update_primal_1 = self._defineoperator(
            [],
            [],
            [[]])

        self.update_primal_1 += self.stream_primal
        self.update_primal_1 += self.stream_grad
        self.update_primal_1.connectouttoin(0, (1, 0))

        if not TV:
            self.stream_update_v = self._defineoperator(
                [self.update_v],
                [self.grad_shape],
                [[self.grad_shape,
                  self.grad_shape]])

            self.update_primal_2 = self._defineoperator(
                [],
                [],
                [[]],
                reverse_dir=True)

            self.update_primal_2 += self.stream_update_v
            self.update_primal_2 += self.sym_grad_streamed
            self.update_primal_2.connectouttoin(0, (1, 0))

        if TV:
            self.stream_update_z1 = self._defineoperator(
                [self.update_z1],
                [self.grad_shape],
                [[self.grad_shape,
                  self.grad_shape,
                  self.grad_shape]],
                reverse_dir=True,
                posofnorm=[False])
        else:
            self.stream_update_z1 = self._defineoperator(
                [self.update_z1],
                [self.grad_shape],
                [[self.grad_shape,
                  self.grad_shape,
                  self.grad_shape,
                  self.grad_shape,
                  self.grad_shape]],
                reverse_dir=True,
                posofnorm=[False])

        self.stream_update_r = self._defineoperator(
            [self.update_r],
            [self.data_shape],
            [[self.data_shape,
              self.data_shape,
              self.data_shape,
              self.data_shape]],
            slices=self.packs*self.numofpacks,
            reverse_dir=True,
            posofnorm=[False])
        del self.stream_update_v, self.stream_primal
        if not TV:
            self.stream_update_z2 = self._defineoperator(
                [self.update_z2],
                [symgrad_shape],
                [[symgrad_shape,
                  symgrad_shape,
                  symgrad_shape]])

            self.update_dual_2 = self._defineoperator(
                [],
                [],
                [[]],
                posofnorm=[False, True])

            self.update_dual_2 += self.stream_update_z2
            self.update_dual_2 += self.stream_Kyk2
            self.update_dual_2.connectouttoin(0, (1, 0))
            del self.stream_Kyk2, self.stream_update_z2