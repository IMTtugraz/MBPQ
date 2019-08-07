#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" This module holds the classes for IRGN Optimization without streaming.

Attribues:
  DTYPE (complex64):
    Complex working precission. Currently single precission only.
  DTYPE_real (float32):
    Real working precission. Currently single precission only.
"""
from __future__ import division

import numpy as np
import time
from pkg_resources import resource_filename
import pyopencl as cl
import pyopencl.array as clarray
import pyqmri.operator as operator
import pyqmri.solver as optimizer
from pyqmri._helper_fun import CLProgram as Program
import h5py

DTYPE = np.complex64
DTYPE_real = np.float32


class ModelReco:
    """ IRGN Optimization

    This Class performs IRGN Optimization either with TGV or TV regularization
    and a combination of TGV/TV + H1.

    Attributes:
      par (dict): A python dict containing the necessary information to
        setup the object. Needs to contain the number of slices (NSlice),
        number of scans (NScan), image dimensions (dimX, dimY), number of
        coils (NC), sampling points (N) and read outs (NProj)
        a PyOpenCL queue (queue) and the complex coil
        sensitivities (C).
      gn_res ((list of) float):
        The residual values for of each Gauss-Newton step. Each iteration
        appends its value to the list.
    """
    def __init__(self, par, trafo=1, imagespace=False, SMS=0):
        self.par = par
        self._fval_old = 0
        self._fval = 0
        self._fval_init = 0
        self._ctx = par["ctx"][0]
        self._queue = par["queue"][0]
        self.gn_res = []
        self._prg = Program(
            self._ctx,
            open(
                resource_filename(
                    'pyqmri', 'kernels/OpenCL_Kernels.c')).read())
        self._imagespace = imagespace
        if imagespace:
            self._coil_buf = []
            self._op = operator.OperatorImagespace(par, self._prg)
        else:
            self._coil_buf = cl.Buffer(self._ctx,
                                       cl.mem_flags.READ_ONLY |
                                       cl.mem_flags.COPY_HOST_PTR,
                                       hostbuf=self.par["C"].data)
            if SMS:
                self._op = operator.OperatorKspaceSMS(par, self._prg, trafo)
            else:
                self._op = operator.OperatorKspace(par, self._prg, trafo)
            self._FT = self._op.NUFFT.FFT

    def _setupLinearOps(self, TV):
        self.grad_op = operator.OperatorFiniteGradient(self.par, self._prg)
        if TV is not True:
            self.symgrad_op = operator.OperatorFiniteSymGradient(
                self.par, self._prg)
            self.v = np.zeros(
                ([self.par["unknowns"], self.par["NSlice"],
                  self.par["dimY"], self.par["dimX"], 4]),
                dtype=DTYPE)

    def _setupPDOptimizer(self, TV):
        if TV == 1:
            self.pdop = optimizer.PDSolver(self.par, self.irgn_par,
                                           self._queue,
                                           np.float32(1 / np.sqrt(8)),
                                           self._fval_init, self._prg, TV)
            self.pdop.grad_op = self.grad_op
        elif TV == 0:
            L = np.float32(0.5 * (18.0 + np.sqrt(33)))
            self.pdop = optimizer.PDSolver(self.par, self.irgn_par,
                                           self._queue,
                                           np.float32(1 / np.sqrt(L)),
                                           self._fval_init, self._prg, TV)
            self.pdop.grad_op = self.grad_op
            self.pdop.symgrad_op = self.symgrad_op
        else:
            L = np.float32(0.5 * (18.0 + np.sqrt(33)))
            self.pdop = optimizer.PDSolver(self.par, self.irgn_par,
                                           self._queue,
                                           np.float32(1 / np.sqrt(L)),
                                           self._fval_init, self._prg, TV)
            self.pdop.grad_op = self.grad_op
            self.pdop.symgrad_op = self.symgrad_op

        self.pdop._coil_buf = self._coil_buf
        self.pdop._op = self._op
        self.pdop.model = self.model

###############################################################################
# Start a 3D Reconstruction, set TV to True to perform TV instead of TGV#######
# Precompute Model and Gradient values for xk #################################
# Call inner optimization #####################################################
# input: bool to switch between TV (1) and TGV (0) regularization #############
# output: optimal value of x ##################################################
###############################################################################
    def _executeIRGN3D(self, TV=0):
        iters = self.irgn_par["start_iters"]

        result = np.copy(self.model.guess)

        self._setupLinearOps(TV)
        self.step_val = np.nan_to_num(self.model.execute_forward(result))
        self._calcResidual(result, self.data, 0, TV)
        self._setupPDOptimizer(TV)

        for ign in range(self.irgn_par["max_gn_it"]):
            start = time.time()
            self.grad_x = np.nan_to_num(self.model.execute_gradient(result))
            self._balanceModelGradients(result, ign)
            self.pdop.grad_op.updateRatio(result)

            self.step_val = np.nan_to_num(self.model.execute_forward(result))
            self.grad_buf = cl.Buffer(self._ctx,
                                      cl.mem_flags.READ_ONLY |
                                      cl.mem_flags.COPY_HOST_PTR,
                                      hostbuf=self.grad_x.data)
            self.pdop.grad_buf = self.grad_buf

            self._updateIRGNRegPar(result, ign)
            self.pdop.updateRegPar(self.irgn_par)

            result = self._irgnSolve3D(result, iters, self.data, ign, TV)

            iters = np.fmin(iters * 2, self.irgn_par["max_iters"])

            end = time.time() - start
            self.gn_res.append(self._fval)
            print("-" * 75)
            print("GN-Iter: %d  Elapsed time: %f seconds" % (ign, end))
            print("-" * 75)
            if np.abs(self._fval_old - self._fval) / self._fval_init < \
               self.irgn_par["tol"]:
                print("Terminated at GN-iteration %d because "
                      "the energy decrease was less than %.3e" %
                      (ign,  np.abs(self._fval_old - self._fval) /
                       self._fval_init))
                self._calcResidual(result, self.data, ign+1, TV)
                self._saveToFile(ign, self.model.rescale(result), TV)
                break
            self._fval_old = self._fval
            self._saveToFile(ign, self.model.rescale(result), TV)
        self._calcResidual(result, self.data, ign+1, TV)

    def _updateIRGNRegPar(self, result, ign):
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

    def _balanceModelGradients(self, result, ind):
        scale = np.reshape(
            self.grad_x,
            (self.par["unknowns"],
             self.par["NScan"] * self.par["NSlice"] *
             self.par["dimY"] * self.par["dimX"]))
        scale = np.linalg.norm(scale, axis=-1)
        print("Initial norm of the model Gradient: \n", scale)
        scale = 1e3 / np.sqrt(self.par["unknowns"]) / scale
        print("Scalefactor of the model Gradient: \n", scale)
        if not np.mod(ind, 1):
            for uk in range(self.par["unknowns"]):
                self.model.constraints[uk].update(scale[uk])
                result[uk, ...] *= self.model.uk_scale[uk]
                self.grad_x[uk] /= self.model.uk_scale[uk]
                self.model.uk_scale[uk] *= scale[uk]
                result[uk, ...] /= self.model.uk_scale[uk]
                self.grad_x[uk] *= self.model.uk_scale[uk]
        scale = np.reshape(
            self.grad_x,
            (self.par["unknowns"],
             self.par["NScan"] * self.par["NSlice"] *
             self.par["dimY"] * self.par["dimX"]))
        scale = np.linalg.norm(scale, axis=-1)
        print("Scale of the model Gradient: \n", scale)

###############################################################################
# New .hdf5 save files ########################################################
###############################################################################
    def _saveToFile(self, myit, result, TV):
        f = h5py.File(self.par["outdir"]+"output_" + self.par["fname"], "a")
        if not TV:
            f.create_dataset("tgv_result_iter_"+str(myit), result.shape,
                             dtype=DTYPE, data=result)
            f.attrs['res_tgv_iter_'+str(myit)] = self._fval
        else:
            f.create_dataset("tv_result_"+str(myit), result.shape,
                             dtype=DTYPE, data=result)
            f.attrs['res_tv_iter_'+str(myit)] = self._fval
        f.close()

###############################################################################
# Precompute constant terms of the GN linearization step ######################
# input: linearization point x ################################################
# iters: number of innner iterations iters ####################################
# data: the input data ########################################################
# TV: bool to switch between TV (1) and TGV (0) regularization ################
# output: optimal value of x for the inner GN step ############################
###############################################################################
###############################################################################
    def _irgnSolve3D(self, x, iters, data, GN_it, TV=0):
        x = clarray.to_device(self._queue, np.require(x, requirements="C"))
        if self._imagespace is False:
            b = clarray.empty(self._queue, data.shape, dtype=DTYPE)
            self._FT(b, clarray.to_device(
                self._queue,
                self.step_val[:, None, ...] * self.par["C"])).wait()
            res = data - b.get() + self._op.fwdoop(
                [x, self._coil_buf, self.grad_buf]).get()
        else:
            res = data - self.step_val + self._op.fwdoop(
                [x, self.grad_buf]).get()
        x = x.get()
        if GN_it > 0:
            self._calcResidual(x, data, GN_it, TV)

        (x, v) = self.pdop.run(x, res, iters)

        return x

    def _calcResidual(self, x, data, GN_it, TV=0):
        x = clarray.to_device(self._queue, np.require(x, requirements="C"))
        if self._imagespace is False:
            b = clarray.empty(self._queue, data.shape, dtype=DTYPE)
            self._FT(b, clarray.to_device(
                            self._queue,
                            (self.step_val[:, None, ...] *
                             self.par["C"]))).wait()
            b = b.get()
        else:
            b = self.step_val
        grad = clarray.to_device(self._queue,
                                 np.zeros(x.shape+(4,), dtype=DTYPE))
        grad.add_event(
            self.grad_op.fwd(
                grad,
                x,
                wait_for=grad.events +
                x.events))
        x = x.get()
        grad = grad.get()
        if TV == 1:
            self._fval = (self.irgn_par["lambd"] / 2 *
                          np.linalg.norm(data - b)**2 +
                          self.irgn_par["gamma"] *
                          np.sum(np.abs(grad[:self.par["unknowns_TGV"]])) +
                          self.irgn_par["omega"] / 2 *
                          np.linalg.norm(grad[self.par["unknowns_TGV"]:])**2)

        elif TV == 0:
            v = clarray.to_device(self._queue, self.v)
            sym_grad = clarray.to_device(self._queue,
                                         np.zeros(x.shape+(8,), dtype=DTYPE))
            sym_grad.add_event(
                self.symgrad_op.fwd(
                    sym_grad,
                    v,
                    wait_for=sym_grad.events +
                    v.events))
            self._fval = (self.irgn_par["lambd"] / 2 *
                          np.linalg.norm(data - b)**2 +
                          self.irgn_par["gamma"] *
                          np.sum(np.abs(grad[:self.par["unknowns_TGV"]] -
                                        self.v)) +
                          self.irgn_par["gamma"] * (2) *
                          np.sum(np.abs(sym_grad.get())) +
                          self.irgn_par["omega"] / 2 *
                          np.linalg.norm(grad[self.par["unknowns_TGV"]:])**2)
            del sym_grad, v
        else:
            v = clarray.to_device(self._queue, self.v)
            sym_grad = clarray.to_device(self._queue,
                                         np.zeros(x.shape+(8,), dtype=DTYPE))
            sym_grad.add_event(
                self.symgrad_op.fwd(
                    sym_grad,
                    v,
                    wait_for=sym_grad.events +
                    v.events))
            self._fval = (self.irgn_par["lambd"] / 2 *
                          np.linalg.norm(data - b)**2 +
                          self.irgn_par["gamma"] *
                          np.sum(np.abs(grad[:self.par["unknowns_TGV"]] -
                                        self.v)) +
                          self.irgn_par["gamma"] *
                          2 * np.sum(np.abs(sym_grad.get())) +
                          self.irgn_par["omega"] / 2 *
                          np.linalg.norm(grad[self.par["unknowns_TGV"]:])**2)
            del sym_grad, v

        del grad, b
        if GN_it == 0:
            self._fval_init = self._fval
        print("-" * 75)
        print("Function value at GN-Step %i: %f" %
              (GN_it, 1e3*self._fval / self._fval_init))
        print("-" * 75)

    def execute(self, TV=0, imagespace=0, reco_2D=0):
        if reco_2D:
            print("2D currently not implemented, \
                  3D can be used with a single slice.")
            raise NotImplementedError
        else:
            self.irgn_par["lambd"] *= self.par["SNR_est"]
            self.delta = self.irgn_par["delta"]
            self.delta_max = self.irgn_par["delta_max"]
            self.gamma = self.irgn_par["gamma"]
            self.omega = self.irgn_par["omega"]
            self._executeIRGN3D(TV)