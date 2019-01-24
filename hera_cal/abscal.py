# -*- coding: utf-8 -*-
# Copyright 2018 the HERA Project
# Licensed under the MIT License
"""
abscal.py
---------

Calibrate measured visibility
data to a visibility model using
linearizations of the (complex)
antenna-based calibration equation:

V_ij,xy^data = g_i_x * conj(g_j_y) * V_ij,xy^model.

Complex-valued parameters are broken into amplitudes and phases as:

V_ij,xy^model = exp(eta_ij,xy^model + i * phi_ij,xy^model)
g_i_x = exp(eta_i_x + i * phi_i_x)
g_j_y = exp(eta_j_y + i * phi_j_y)
V_ij,xy^data = exp(eta_ij,xy^data + i * phi_ij,xy^data)

where {i,j} index antennas and {x,y} are the polarization of
the i-th and j-th antenna respectively.
"""
from __future__ import print_function, division, absolute_import

import gc as garbage_collector
from six.moves import map, range

from . import version
from . import flag_utils
from .apply_cal import calibrate_in_place
from .smooth_cal import pick_reference_antenna
from .flag_utils import synthesize_ant_flags
from .noise import predict_noise_variance_from_autos
from .abscal_funcs import *


class AbsCal(object):
    """
    AbsCal object used to for phasing and scaling visibility data to an absolute reference model.
    A few different calibration methods exist. These include:

    1) per-antenna amplitude logarithmic calibration solves the equation:
            ln[abs(V_ij^data / V_ij^model)] = eta_i + eta_j

    2) per-antenna phase logarithmic calibration solves the equation:
           angle(V_ij^data / V_ij^model) = phi_i - phi_j

    3) delay linear calibration solves the equation:
           delay(V_ij^data / V_ij^model) = delay(g_i) - delay(g_j)
                                         = tau_i - tau_j
       where tau is the delay that can be turned
       into a complex gain via: g = exp(i * 2pi * tau * freqs).

    4) delay slope linear calibration solves the equation:
            delay(V_ij^data / V_ij^model) = dot(T_dly, B_ij)
        where T_dly is a delay slope in [ns / meter]
        and B_ij is the baseline vector between ant i and j.

    5) frequency-independent phase slope calibration
        median_over_freq(angle(V_ij^data / V_ij^model)) = dot(Phi, B_ji)
        where Phi is a phase slope in [radians / meter]
        and B_ij is the baseline vector between ant i and j.

    6) Average amplitude linear calibration solves the equation:
            log|V_ij^data / V_ij^model| = log|g_avg_i| + log|g_avg_j|

    7) Tip-Tilt phase logarithmic calibration solves the equation
            angle(V_ij^data /  V_ij^model) = psi + dot(TT_Phi, B_ij)
        where psi is an overall gain phase scalar,
        TT_Phi is the gain phase slope vector [radians / meter]
        and B_ij is the baseline vector between antenna i and j.

    Methods (1), (2) and (3) can be thought of as general bandpass solvers, whereas
    methods (4), (5), (6), and (7) are methods that would be used for data that has already
    been redundantly calibrated.

    Be warned that the linearizations of the phase solvers suffer from phase wrapping
    pathologies, meaning that a delay calibration should generally precede a
    phs_logcal or a TT_phs_logcal bandpass routine.
    """
    def __init__(self, model, data, refant=None, wgts=None, antpos=None, freqs=None,
                 min_bl_cut=None, max_bl_cut=None, bl_taper_fwhm=None, verbose=True,
                 filetype='miriad', input_cal=None):
        """
        AbsCal object used to for phasing and scaling visibility data to an absolute reference model.

        The format of model, data and wgts is in a dictionary format, with the convention that
        keys contain antennas-pairs + polarization, Ex. (1, 2, 'xx'), and values contain 2D complex
        ndarrays with [0] axis indexing time and [1] axis frequency.

        Parameters:
        -----------
        model : Visibility data of refence model, type=dictionary or DataContainer
                keys are antenna-pair + polarization tuples, Ex. (1, 2, 'xx').
                values are complex ndarray visibilities.
                these must be 2D arrays, with [0] axis indexing time
                and [1] axis indexing frequency.

                Optionally, model can be a path to a pyuvdata-supported file, a
                pyuvdata.UVData object or hera_cal.HERAData object,
                or a list of either.

        data :  Visibility data, type=dictionary or DataContainer
                keys are antenna-pair + polarization tuples, Ex. (1, 2, 'xx').
                values are complex ndarray visibilities.
                these must be 2D arrays, with [0] axis indexing time
                and [1] axis indexing frequency.

                Optionally, data can be a path to a pyuvdata-supported file, a
                pyuvdata.UVData object or hera_cal.HERAData object,
                or a list of either. In this case, antpos, freqs
                and wgts are overwritten from arrays in data.

        refant : antenna number integer for reference antenna
            The refence antenna is used in the phase solvers, where an absolute phase is applied to all
            antennas such that the refant's phase is set to identically zero.

        wgts : weights of the data, type=dictionary or DataContainer, [default=None]
               keys are antenna pair + pol tuples (must match model), values are real floats
               matching shape of model and data

        antpos : type=dictionary, dict of antenna position vectors in ENU (topo) frame in meters.
                 origin of coordinates does not matter, but preferably are centered in the array.
                 keys are antenna integers and values are ndarray position vectors,
                 containing [East, North, Up] coordinates.
                 Can be generated from a pyuvdata.UVData instance via
                 ----
                 #!/usr/bin/env python
                 uvd = pyuvdata.UVData()
                 uvd.read_miriad(<filename>)
                 antenna_pos, ants = uvd.get_ENU_antpos()
                 antpos = dict(zip(ants, antenna_pos))
                 ----
                 This is needed only for Tip Tilt, phase slope, and delay slope calibration.

        freqs : ndarray of frequency array, type=ndarray
                1d array containing visibility frequencies in Hz.
                Needed for delay calibration.

        min_bl_cut : float, eliminate all visibilities with baseline separation lengths
            smaller than min_bl_cut. This is assumed to be in ENU coordinates with units of meters.

        max_bl_cut : float, eliminate all visibilities with baseline separation lengths
            larger than max_bl_cut. This is assumed to be in ENU coordinates with units of meters.

        bl_taper_fwhm : float, impose a gaussian taper on the data weights as a function of
            bl separation length, with a specified fwhm [meters]

        filetype : str, if data and/or model are fed as strings, this is their filetype

        input_cal : filepath to calfits, UVCal or HERACal object with gain solutions to
            apply to data on-the-fly via hera_cal.apply_cal.calibrate_in_place
        """
        # set pols to None
        pols = None

        # load model if necessary
        if isinstance(model, list) or isinstance(model, np.ndarray) or isinstance(model, str) or issubclass(model.__class__, UVData):
            (model, model_flags, model_antpos, model_ants, model_freqs, model_lsts,
             model_times, model_pols) = io.load_vis(model, pop_autos=True, return_meta=True, filetype=filetype)

        # load data if necessary
        if isinstance(data, list) or isinstance(data, np.ndarray) or isinstance(data, str) or issubclass(data.__class__, UVData):
            (data, flags, data_antpos, data_ants, data_freqs, data_lsts,
             data_times, data_pols) = io.load_vis(data, pop_autos=True, return_meta=True, filetype=filetype)
            pols = data_pols
            freqs = data_freqs
            antpos = data_antpos

        # apply calibration
        if input_cal is not None:
            if 'flags' not in locals():
                flags = None
            uvc = io.to_HERACal(input_cal)
            gains, cal_flags, quals, totquals = uvc.read()
            apply_cal.calibrate_in_place(data, gains, data_flags=flags, cal_flags=cal_flags, gain_convention=uvc.gain_convention)

        # get shared keys
        self.keys = sorted(set(model.keys()) & set(data.keys()))
        assert len(self.keys) > 0, "no shared keys exist between model and data"

        # append attributes
        self.model = DataContainer(dict([(k, model[k]) for k in self.keys]))
        self.data = DataContainer(dict([(k, data[k]) for k in self.keys]))

        # setup frequencies
        self.freqs = freqs
        if self.freqs is None:
            self.Nfreqs = None
        else:
            self.Nfreqs = len(self.freqs)

        # get pols is not defined, if so, make sure they are string format
        if pols is None:
            pols = np.unique(list(map(lambda k: k[2], self.keys)))
        elif isinstance(pols, np.ndarray) or isinstance(pols, list):
            if np.issubdtype(type(pols[0]), int):
                pols = list(map(lambda p: polnum2str(p), pols))

        # convert to integer format
        self.pols = pols
        self.pols = list(map(lambda p: polstr2num(p), self.pols))
        self.Npols = len(self.pols)

        # save pols in string format and get gain_pols
        self.polstrings = np.array(list(map(lambda p: polnum2str(p), self.pols)))
        self.gain_pols = np.unique(list(map(lambda p: list(utils.split_pol(p)), self.polstrings)))
        self.Ngain_pols = len(self.gain_pols)

        # setup weights
        if wgts is None:
            # use data flags if present
            if 'flags' in locals() and flags is not None:
                wgts = DataContainer(dict([(k, (~flags[k]).astype(np.float)) for k in self.keys]))
            else:
                wgts = DataContainer(dict([(k, np.ones_like(data[k], dtype=np.float)) for k in self.keys]))
            if 'model_flags' in locals():
                for k in self.keys:
                    wgts[k] *= (~model_flags[k]).astype(np.float)
        self.wgts = wgts

        # setup ants
        self.ants = np.unique(np.concatenate(list(map(lambda k: k[:2], self.keys))))
        self.Nants = len(self.ants)
        if refant is None:
            refant = self.keys[0][0]
            print("using {} for reference antenna".format(refant))
        else:
            assert refant in self.ants, "refant {} not found in self.ants".format(refant)
        self.refant = refant

        # setup antenna positions
        self.antpos = antpos
        self.antpos_arr = None
        self.bls = None
        if self.antpos is not None:
            # center antpos about reference antenna
            self.antpos = odict(list(map(lambda k: (k, antpos[k] - antpos[self.refant]), self.ants)))
            self.bls = odict([(x, self.antpos[x[0]] - self.antpos[x[1]]) for x in self.keys])
            self.antpos_arr = np.array(list(map(lambda x: self.antpos[x], self.ants)))
            self.antpos_arr -= np.median(self.antpos_arr, axis=0)

        # setup gain solution keys
        self._gain_keys = list(map(lambda p: list(map(lambda a: (a, p), self.ants)), self.gain_pols))

        # perform baseline cut
        if min_bl_cut is not None or max_bl_cut is not None:
            assert self.antpos is not None, "can't request a bl_cut if antpos is not fed"

            _model = cut_bls(self.model, self.bls, min_bl_cut, max_bl_cut)
            _data = cut_bls(self.data, self.bls, min_bl_cut, max_bl_cut)
            _wgts = cut_bls(self.wgts, self.bls, min_bl_cut, max_bl_cut)

            # re-init
            self.__init__(_model, _data, refant=self.refant, wgts=_wgts, antpos=self.antpos, freqs=self.freqs, verbose=verbose)

        # enact a baseline weighting taper
        if bl_taper_fwhm is not None:
            assert self.antpos is not None, "can't request a baseline taper if antpos is not fed"

            # make gaussian taper func
            def taper(ratio):
                return np.exp(-0.5 * ratio**2)

            # iterate over baselines
            for k in self.wgts.keys():
                self.wgts[k] *= taper(np.linalg.norm(self.bls[k]) / bl_taper_fwhm)

    def amp_logcal(self, verbose=True):
        """
        Call abscal_funcs.amp_logcal() method. see its docstring for more details.

        Parameters:
        -----------
        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        per-antenna amplitude and per-antenna amp gains
        can be accessed via the getter functions
            self.ant_eta
            self.ant_eta_arr
            self.ant_eta_gain
            self.ant_eta_gain_arr
        """
        # set data quantities
        model = self.model
        data = self.data
        wgts = copy.copy(self.wgts)

        # run linsolve
        fit = amp_logcal(model, data, wgts=wgts, verbose=verbose)

        # form result array
        self._ant_eta = odict(list(map(lambda k: (k, copy.copy(fit["eta_{}_{}".format(k[0], k[1])])), flatten(self._gain_keys))))
        self._ant_eta_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_eta[k], pk)), self._gain_keys)), 0, -1)

    def phs_logcal(self, avg=False, verbose=True):
        """
        call abscal_funcs.phs_logcal() method. see its docstring for more details.

        Parameters:
        -----------
        avg : type=boolean, if True, average solution across time and frequency

        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        per-antenna phase and per-antenna phase gains
        can be accessed via the methods
            self.ant_phi
            self.ant_phi_arr
            self.ant_phi_gain
            self.ant_phi_gain_arr
        """
        # assign data
        model = self.model
        data = self.data
        wgts = copy.deepcopy(self.wgts)

        # run linsolve
        fit = phs_logcal(model, data, wgts=wgts, refant=self.refant, verbose=verbose)

        # form result array
        self._ant_phi = odict(list(map(lambda k: (k, copy.copy(fit["phi_{}_{}".format(k[0], k[1])])), flatten(self._gain_keys))))
        self._ant_phi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_phi[k], pk)), self._gain_keys)), 0, -1)

        # take time and freq average
        if avg:
            self._ant_phi = odict(list(map(lambda k: (k, np.ones_like(self._ant_phi[k])
                                                      * np.angle(np.median(np.real(np.exp(1j * self._ant_phi[k])))
                                                                 + 1j * np.median(np.imag(np.exp(1j * self._ant_phi[k]))))), flatten(self._gain_keys))))
            self._ant_phi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_phi[k], pk)), self._gain_keys)), 0, -1)

    def delay_lincal(self, medfilt=True, kernel=(1, 11), verbose=True, time_avg=False, edge_cut=0):
        """
        Solve for per-antenna delay according to the equation
        by calling abscal_funcs.delay_lincal method.
        See abscal_funcs.delay_lincal for details.

        Parameters:
        -----------
        medfilt : boolean, if True median filter data before fft

        kernel : size of median filter across (time, freq) axes, type=(int, int)

        time_avg : boolean, if True, average resultant antenna delays across time

        edge_cut : int, number of channels to exclude at each band edge in FFT window

        Result:
        -------
        per-antenna delays, per-antenna delay gains, per-antenna phase + phase gains
        can be accessed via the methods
            self.ant_dly
            self.ant_dly_gain
            self.ant_dly_arr
            self.ant_dly_gain_arr
            self.ant_dly_phi
            self.ant_dly_phi_gain
            self.ant_dly_phi_arr
            self.ant_dly_phi_gain_arr
        """
        # check for freq data
        if self.freqs is None:
            raise AttributeError("cannot delay_lincal without self.freqs array")

        # assign data
        model = self.model
        data = self.data
        wgts = self.wgts

        # get freq channel width
        df = np.median(np.diff(self.freqs))

        # run delay_lincal
        fit = delay_lincal(model, data, wgts=wgts, refant=self.refant, medfilt=medfilt, df=df, 
                           kernel=kernel, verbose=verbose, edge_cut=edge_cut)

        # time average
        if time_avg:
            k = flatten(self._gain_keys)[0]
            Ntimes = fit["tau_{}_{}".format(k[0], k[1])].shape[0]
            for i, k in enumerate(flatten(self._gain_keys)):
                tau_key = "tau_{}_{}".format(k[0], k[1])
                tau_avg = np.moveaxis(np.median(fit[tau_key], axis=0)[np.newaxis], 0, 0)
                fit[tau_key] = np.repeat(tau_avg, Ntimes, axis=0)
                phi_key = "phi_{}_{}".format(k[0], k[1])
                gain = np.exp(1j * fit[phi_key])
                real_avg = np.median(np.real(gain), axis=0)
                imag_avg = np.median(np.imag(gain), axis=0)
                phi_avg = np.moveaxis(np.angle(real_avg + 1j * imag_avg)[np.newaxis], 0, 0)
                fit[phi_key] = np.repeat(phi_avg, Ntimes, axis=0)

        # form result
        self._ant_dly = odict(list(map(lambda k: (k, copy.copy(fit["tau_{}_{}".format(k[0], k[1])])), flatten(self._gain_keys))))
        self._ant_dly_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_dly[k], pk)), self._gain_keys)), 0, -1)

        self._ant_dly_phi = odict(list(map(lambda k: (k, copy.copy(fit["phi_{}_{}".format(k[0], k[1])])), flatten(self._gain_keys))))
        self._ant_dly_phi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._ant_dly_phi[k], pk)), self._gain_keys)), 0, -1)

    def delay_slope_lincal(self, medfilt=True, kernel=(1, 15), verbose=True, time_avg=False,
                           four_pol=False, edge_cut=0):
        """
        Solve for an array-wide delay slope (a subset of the omnical degeneracies) by calling
        abscal_funcs.delay_slope_lincal method. See abscal_funcs.delay_slope_lincal for details.

        Parameters:
        -----------
        medfilt : boolean, if True median filter data before fft

        kernel : size of median filter across (time, freq) axes, type=(int, int)

        verbose : type=boolean, if True print feedback to stdout

        time_avg : boolean, if True, average resultant delay slope across time

        four_pol : boolean, if True, form a joint polarization solution

        edge_cut : int, number of channels to exclude at each band edge in FFT window

        Result:
        -------
        delays slopes, per-antenna delay gains, per-antenna phase + phase gains
        can be accessed via the methods
            self.dly_slope
            self.dly_slope_gain
            self.dly_slope_arr
            self.dly_slope_gain_arr
        """
        # check for freq data
        if self.freqs is None:
            raise AttributeError("cannot delay_slope_lincal without self.freqs array")

        # assign data
        model = self.model
        data = self.data
        wgts = self.wgts
        antpos = self.antpos

        # get freq channel width
        df = np.median(np.diff(self.freqs))

        # run delay_slope_lincal
        fit = delay_slope_lincal(model, data, antpos, wgts=wgts, refant=self.refant, medfilt=medfilt, df=df,
                                 kernel=kernel, verbose=verbose, four_pol=four_pol, edge_cut=edge_cut)

        # separate pols if four_pol
        if four_pol:
            for i, gp in enumerate(self.gain_pols):
                fit['T_ew_{}'.format(gp)] = fit["T_ew"]
                fit['T_ns_{}'.format(gp)] = fit["T_ns"]
                fit.pop('T_ew')
                fit.pop('T_ns')

        # time average
        if time_avg:
            k = flatten(self._gain_keys)[0]
            Ntimes = fit["T_ew_{}".format(k[1])].shape[0]
            for i, k in enumerate(flatten(self._gain_keys)):
                ew_key = "T_ew_{}".format(k[1])
                ns_key = "T_ns_{}".format(k[1])
                ew_avg = np.moveaxis(np.median(fit[ew_key], axis=0)[np.newaxis], 0, 0)
                ns_avg = np.moveaxis(np.median(fit[ns_key], axis=0)[np.newaxis], 0, 0)
                fit[ew_key] = np.repeat(ew_avg, Ntimes, axis=0)
                fit[ns_key] = np.repeat(ns_avg, Ntimes, axis=0)

        # form result
        self._dly_slope = odict(list(map(lambda k: (k, copy.copy(np.array([fit["T_ew_{}".format(k[1])], fit["T_ns_{}".format(k[1])]]))), flatten(self._gain_keys))))
        self._dly_slope_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: np.array([self._dly_slope[k][0], self._dly_slope[k][1]]), pk)), self._gain_keys)), 0, -1)

    def global_phase_slope_logcal(self, tol=1.0, edge_cut=0, verbose=True):
        """
        Solve for a frequency-independent spatial phase slope (a subset of the omnical degeneracies) by calling
        abscal_funcs.global_phase_slope_logcal method. See abscal_funcs.global_phase_slope_logcal for details.

        Parameters:
        -----------
        tol : type=float, baseline match tolerance in units of baseline vectors (e.g. meters)

        edge_cut : int, number of channels to exclude at each band edge in phase slope solver

        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        per-antenna delays, per-antenna delay gains, per-antenna phase + phase gains
        can be accessed via the methods
            self.phs_slope
            self.phs_slope_gain
            self.phs_slope_arr
            self.phs_slope_gain_arr
        """

        # assign data
        model = self.model
        data = self.data
        wgts = self.wgts
        antpos = self.antpos

        # run global_phase_slope_logcal
        fit = global_phase_slope_logcal(model, data, antpos, wgts=wgts, refant=self.refant, verbose=verbose, tol=tol, edge_cut=edge_cut)

        # form result
        self._phs_slope = odict(list(map(lambda k: (k, copy.copy(np.array([fit["Phi_ew_{}".format(k[1])], fit["Phi_ns_{}".format(k[1])]]))), flatten(self._gain_keys))))
        self._phs_slope_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: np.array([self._phs_slope[k][0], self._phs_slope[k][1]]), pk)), self._gain_keys)), 0, -1)

    def abs_amp_logcal(self, verbose=True):
        """
        call abscal_funcs.abs_amp_logcal() method. see its docstring for more details.

        Parameters:
        -----------
        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        Absolute amplitude scalar can be accessed via methods
            self.abs_eta
            self.abs_eta_gain
            self.abs_eta_arr
            self.abs_eta_gain_arr
        """
        # set data quantities
        model = self.model
        data = self.data
        wgts = self.wgts

        # run abs_amp_logcal
        fit = abs_amp_logcal(model, data, wgts=wgts, verbose=verbose)

        # form result
        self._abs_eta = odict(list(map(lambda k: (k, copy.copy(fit["eta_{}".format(k[1])])), flatten(self._gain_keys))))
        self._abs_eta_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._abs_eta[k], pk)), self._gain_keys)), 0, -1)

    def TT_phs_logcal(self, verbose=True, zero_psi=True, four_pol=False):
        """
        call abscal_funcs.TT_phs_logcal() method. see its docstring for more details.

        Parameters:
        -----------
        zero_psi : type=boolean, set overall gain phase (psi) to identically zero in linsolve equations.
            This is separate than the reference antenna's absolute phase being set to zero, as it can account
            for absolute phase offsets between polarizations.

        four_pol : type=boolean, even if multiple polarizations are present in data, make free
                    variables polarization un-aware: i.e. one solution across all polarizations.
                    This is the same assumption as 4-polarization calibration in omnical.

        verbose : type=boolean, if True print feedback to stdout

        Result:
        -------
        Tip-Tilt phase slope and overall phase fit can be accessed via methods
            self.abs_psi
            self.abs_psi_gain
            self.TT_Phi
            self.TT_Phi_gain
            self.abs_psi_arr
            self.abs_psi_gain_arr
            self.TT_Phi_arr
            self.TT_Phi_gain_arr
        """
        # set data quantities
        model = self.model
        data = self.data
        wgts = self.wgts
        antpos = self.antpos

        # run TT_phs_logcal
        fit = TT_phs_logcal(model, data, antpos, wgts=wgts, refant=self.refant, verbose=verbose, zero_psi=zero_psi, four_pol=four_pol)

        # manipulate if four_pol
        if four_pol:
            for i, gp in enumerate(self.gain_pols):
                fit['Phi_ew_{}'.format(gp)] = fit["Phi_ew"]
                fit['Phi_ns_{}'.format(gp)] = fit["Phi_ns"]
                fit.pop('Phi_ew')
                fit.pop('Phi_ns')

        # form result
        self._abs_psi = odict(list(map(lambda k: (k, copy.copy(fit["psi_{}".format(k[1])])), flatten(self._gain_keys))))
        self._abs_psi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: self._abs_psi[k], pk)), self._gain_keys)), 0, -1)

        self._TT_Phi = odict(list(map(lambda k: (k, copy.copy(np.array([fit["Phi_ew_{}".format(k[1])], fit["Phi_ns_{}".format(k[1])]]))), flatten(self._gain_keys))))
        self._TT_Phi_arr = np.moveaxis(list(map(lambda pk: list(map(lambda k: np.array([self._TT_Phi[k][0], self._TT_Phi[k][1]]), pk)), self._gain_keys)), 0, -1)

    # amp_logcal results
    @property
    def ant_eta(self):
        """ return _ant_eta dict, containing per-antenna amplitude solution """
        if hasattr(self, '_ant_eta'):
            return copy.deepcopy(self._ant_eta)
        else:
            return None

    @property
    def ant_eta_gain(self):
        """ form complex gain from _ant_eta dict """
        if hasattr(self, '_ant_eta'):
            ant_eta = self.ant_eta
            return odict(list(map(lambda k: (k, np.exp(ant_eta[k]).astype(np.complex)), flatten(self._gain_keys))))
        else:
            return None

    @property
    def ant_eta_arr(self):
        """ return _ant_eta in ndarray format """
        if hasattr(self, '_ant_eta_arr'):
            return copy.copy(self._ant_eta_arr)
        else:
            return None

    @property
    def ant_eta_gain_arr(self):
        """ return _ant_eta_gain in ndarray format """
        if hasattr(self, '_ant_eta_arr'):
            return np.exp(self.ant_eta_arr).astype(np.complex)
        else:
            return None

    # phs_logcal results
    @property
    def ant_phi(self):
        """ return _ant_phi dict, containing per-antenna phase solution """
        if hasattr(self, '_ant_phi'):
            return copy.deepcopy(self._ant_phi)
        else:
            return None

    @property
    def ant_phi_gain(self):
        """ form complex gain from _ant_phi dict """
        if hasattr(self, '_ant_phi'):
            ant_phi = self.ant_phi
            return odict(list(map(lambda k: (k, np.exp(1j * ant_phi[k])), flatten(self._gain_keys))))
        else:
            return None

    @property
    def ant_phi_arr(self):
        """ return _ant_phi in ndarray format """
        if hasattr(self, '_ant_phi_arr'):
            return copy.copy(self._ant_phi_arr)
        else:
            return None

    @property
    def ant_phi_gain_arr(self):
        """ return _ant_phi_gain in ndarray format """
        if hasattr(self, '_ant_phi_arr'):
            return np.exp(1j * self.ant_phi_arr)
        else:
            return None

    # delay_lincal results
    @property
    def ant_dly(self):
        """ return _ant_dly dict, containing per-antenna delay solution """
        if hasattr(self, '_ant_dly'):
            return copy.deepcopy(self._ant_dly)
        else:
            return None

    @property
    def ant_dly_gain(self):
        """ form complex gain from _ant_dly dict """
        if hasattr(self, '_ant_dly'):
            ant_dly = self.ant_dly
            return odict(list(map(lambda k: (k, np.exp(2j * np.pi * self.freqs.reshape(1, -1) * ant_dly[k])), flatten(self._gain_keys))))
        else:
            return None

    @property
    def ant_dly_arr(self):
        """ return _ant_dly in ndarray format """
        if hasattr(self, '_ant_dly_arr'):
            return copy.copy(self._ant_dly_arr)
        else:
            return None

    @property
    def ant_dly_gain_arr(self):
        """ return ant_dly_gain in ndarray format """
        if hasattr(self, '_ant_dly_arr'):
            return np.exp(2j * np.pi * self.freqs.reshape(-1, 1) * self.ant_dly_arr)
        else:
            return None

    @property
    def ant_dly_phi(self):
        """ return _ant_dly_phi dict, containing a single phase solution per antenna """
        if hasattr(self, '_ant_dly_phi'):
            return copy.deepcopy(self._ant_dly_phi)
        else:
            return None

    @property
    def ant_dly_phi_gain(self):
        """ form complex gain from _ant_dly_phi dict """
        if hasattr(self, '_ant_dly_phi'):
            ant_dly_phi = self.ant_dly_phi
            return odict(list(map(lambda k: (k, np.exp(1j * np.repeat(ant_dly_phi[k], self.Nfreqs, 1))), flatten(self._gain_keys))))
        else:
            return None

    @property
    def ant_dly_phi_arr(self):
        """ return _ant_dly_phi in ndarray format """
        if hasattr(self, '_ant_dly_phi_arr'):
            return copy.copy(self._ant_dly_phi_arr)
        else:
            return None

    @property
    def ant_dly_phi_gain_arr(self):
        """ return _ant_dly_phi_gain in ndarray format """
        if hasattr(self, '_ant_dly_phi_arr'):
            return np.exp(1j * np.repeat(self.ant_dly_phi_arr, self.Nfreqs, 2))
        else:
            return None

    # delay_slope_lincal results
    @property
    def dly_slope(self):
        """ return _dly_slope dict, containing the delay slope across the array """
        if hasattr(self, '_dly_slope'):
            return copy.deepcopy(self._dly_slope)
        else:
            return None

    @property
    def dly_slope_gain(self):
        """ form a per-antenna complex gain from _dly_slope dict and the antpos dictionary attached to the class"""
        if hasattr(self, '_dly_slope'):
            # get dly_slope dictionary
            dly_slope = self.dly_slope
            # turn delay slope into per-antenna complex gains, while iterating over self._gain_keys
            return odict(list(map(lambda k: (k, np.exp(2j * np.pi * self.freqs.reshape(1, -1) * np.einsum("i...,i->...", dly_slope[k], self.antpos[k[0]][:2]))),
                                  flatten(self._gain_keys))))
        else:
            return None

    def custom_dly_slope_gain(self, gain_keys, antpos):
        """
        return dly_slope_gain with custom gain keys and antenna positions

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'x'), (1, 'x'), (0, 'y'), (1, 'y')]
        antpos : type=dictionary, contains antenna position vectors. keys are ant integer, values are ant position vectors
        """
        if hasattr(self, '_dly_slope'):
            # get dly slope dictionary
            dly_slope = self.dly_slope[self._gain_keys[0][0]]
            # turn delay slope into per-antenna complex gains, while iterating over gain_keys
            return odict(list(map(lambda k: (k, np.exp(2j * np.pi * self.freqs.reshape(1, -1) * np.einsum("i...,i->...", dly_slope, antpos[k[0]][:2]))),
                                  gain_keys)))
        else:
            return None

    @property
    def dly_slope_arr(self):
        """ return _dly_slope_arr array """
        if hasattr(self, '_dly_slope_arr'):
            return copy.copy(self._dly_slope_arr)
        else:
            return None

    @property
    def dly_slope_gain_arr(self):
        """ form complex gain from _dly_slope_arr array """
        if hasattr(self, '_dly_slope_arr'):
            return np.exp(2j * np.pi * self.freqs.reshape(-1, 1) * np.einsum("hi...,hi->h...", self._dly_slope_arr, self.antpos_arr[:, :2]))
        else:
            return None

    @property
    def dly_slope_ant_dly_arr(self):
        """ form antenna delays from _dly_slope_arr array """
        if hasattr(self, '_dly_slope_arr'):
            return np.einsum("hi...,hi->h...", self._dly_slope_arr, self.antpos_arr[:, :2])
        else:
            return None

    # global_phase_slope_logcal results
    @property
    def phs_slope(self):
        """ return _phs_slope dict, containing the frequency-indpendent phase slope across the array """
        if hasattr(self, '_phs_slope'):
            return copy.deepcopy(self._phs_slope)
        else:
            return None

    @property
    def phs_slope_gain(self):
        """ form a per-antenna complex gain from _phs_slope dict and the antpos dictionary attached to the class"""
        if hasattr(self, '_phs_slope'):
            # get phs_slope dictionary
            phs_slope = self.phs_slope
            # turn phs slope into per-antenna complex gains, while iterating over self._gain_keys
            return odict(list(map(lambda k: (k, np.exp(1.0j * np.ones_like(self.freqs).reshape(1, -1) * np.einsum("i...,i->...", phs_slope[k], self.antpos[k[0]][:2]))),
                                  flatten(self._gain_keys))))
        else:
            return None

    def custom_phs_slope_gain(self, gain_keys, antpos):
        """
        return phs_slope_gain with custom gain keys and antenna positions

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'x'), (1, 'x'), (0, 'y'), (1, 'y')]
        antpos : type=dictionary, contains antenna position vectors. keys are ant integer, values are ant position vectors
        """
        if hasattr(self, '_phs_slope'):
            # get phs slope dictionary
            phs_slope = self.phs_slope[self._gain_keys[0][0]]
            # turn phs slope into per-antenna complex gains, while iterating over gain_keys
            return odict(list(map(lambda k: (k, np.exp(1.0j * np.ones_like(self.freqs).reshape(1, -1) * np.einsum("i...,i->...", phs_slope, antpos[k[0]][:2]))),
                                  gain_keys)))
        else:
            return None

    @property
    def phs_slope_arr(self):
        """ return _phs_slope_arr array """
        if hasattr(self, '_phs_slope_arr'):
            return copy.copy(self._phs_slope_arr)
        else:
            return None

    @property
    def phs_slope_gain_arr(self):
        """ form complex gain from _phs_slope_arr array """
        if hasattr(self, '_phs_slope_arr'):
            return np.exp(1.0j * np.ones_like(self.freqs).reshape(-1, 1) * np.einsum("hi...,hi->h...", self._phs_slope_arr, self.antpos_arr[:, :2]))
        else:
            return None

    @property
    def phs_slope_ant_phs_arr(self):
        """ form antenna delays from _phs_slope_arr array """
        if hasattr(self, '_phs_slope_arr'):
            return np.einsum("hi...,hi->h...", self._phs_slope_arr, self.antpos_arr[:, :2])
        else:
            return None

    # abs_amp_logcal results
    @property
    def abs_eta(self):
        """return _abs_eta dict"""
        if hasattr(self, '_abs_eta'):
            return copy.deepcopy(self._abs_eta)
        else:
            return None

    @property
    def abs_eta_gain(self):
        """form complex gain from _abs_eta dict"""
        if hasattr(self, '_abs_eta'):
            abs_eta = self.abs_eta
            return odict(list(map(lambda k: (k, np.exp(abs_eta[k]).astype(np.complex)), flatten(self._gain_keys))))
        else:
            return None

    def custom_abs_eta_gain(self, gain_keys):
        """
        return abs_eta_gain with custom gain keys

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'x'), (1, 'x'), (0, 'y'), (1, 'y')]
        """
        if hasattr(self, '_abs_eta'):
            abs_eta = self.abs_eta[self._gain_keys[0][0]]
            return odict(list(map(lambda k: (k, np.exp(abs_eta).astype(np.complex)), gain_keys)))
        else:
            return None

    @property
    def abs_eta_arr(self):
        """return _abs_eta_arr array"""
        if hasattr(self, '_abs_eta_arr'):
            return copy.copy(self._abs_eta_arr)
        else:
            return None

    @property
    def abs_eta_gain_arr(self):
        """form complex gain from _abs_eta_arr array"""
        if hasattr(self, '_abs_eta_arr'):
            return np.exp(self._abs_eta_arr).astype(np.complex)
        else:
            return None

    # TT_phs_logcal results
    @property
    def abs_psi(self):
        """return _abs_psi dict"""
        if hasattr(self, '_abs_psi'):
            return copy.deepcopy(self._abs_psi)
        else:
            return None

    @property
    def abs_psi_gain(self):
        """ form complex gain from _abs_psi array """
        if hasattr(self, '_abs_psi'):
            abs_psi = self.abs_psi
            return odict(list(map(lambda k: (k, np.exp(1j * abs_psi[k])), flatten(self._gain_keys))))
        else:
            return None

    def custom_abs_psi_gain(self, gain_keys):
        """
        return abs_psi_gain with custom gain keys

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'x'), (1, 'x'), (0, 'y'), (1, 'y')]
        """
        if hasattr(self, '_abs_psi'):
            abs_psi = self.abs_psi[self._gain_keys[0][0]]
            return odict(list(map(lambda k: (k, np.exp(1j * abs_psi)), gain_keys)))
        else:
            return None

    @property
    def abs_psi_arr(self):
        """return _abs_psi_arr array"""
        if hasattr(self, '_abs_psi_arr'):
            return copy.copy(self._abs_psi_arr)
        else:
            return None

    @property
    def abs_psi_gain_arr(self):
        """ form complex gain from _abs_psi_arr array """
        if hasattr(self, '_abs_psi_arr'):
            return np.exp(1j * self._abs_psi_arr)
        else:
            return None

    @property
    def TT_Phi(self):
        """return _TT_Phi array"""
        if hasattr(self, '_TT_Phi'):
            return copy.deepcopy(self._TT_Phi)
        else:
            return None

    @property
    def TT_Phi_gain(self):
        """ form complex gain from _TT_Phi array """
        if hasattr(self, '_TT_Phi'):
            TT_Phi = self.TT_Phi
            return odict(list(map(lambda k: (k, np.exp(1j * np.einsum("i...,i->...", TT_Phi[k], self.antpos[k[0]][:2]))), flatten(self._gain_keys))))
        else:
            return None

    def custom_TT_Phi_gain(self, gain_keys, antpos):
        """
        return TT_Phi_gain with custom gain keys and antenna positions

        gain_keys : type=list, list of unique (ant, pol). Ex. [(0, 'x'), (1, 'x'), (0, 'y'), (1, 'y')]
        antpos : type=dictionary, contains antenna position vectors. keys are ant integer, values are ant positions
        """
        if hasattr(self, '_TT_Phi'):
            TT_Phi = self.TT_Phi[self._gain_keys[0][0]]
            return odict(list(map(lambda k: (k, np.exp(1j * np.einsum("i...,i->...", TT_Phi, antpos[k[0]][:2]))), gain_keys)))
        else:
            return None

    @property
    def TT_Phi_arr(self):
        """return _TT_Phi_arr array"""
        if hasattr(self, '_TT_Phi_arr'):
            return copy.copy(self._TT_Phi_arr)
        else:
            return None

    @property
    def TT_Phi_gain_arr(self):
        """ form complex gain from _TT_Phi_arr array """
        if hasattr(self, '_TT_Phi_arr'):
            return np.exp(1j * np.einsum("hi...,hi->h...", self._TT_Phi_arr, self.antpos_arr[:, :2]))
        else:
            return None


def abscal_arg_parser():
    """
    argparser for general abscal run. By default no calibration is performed: the user
    needs to specify which calibration steps they want via the delay_cal, avg_phs_cal,
    delay_slope_cal, phase_slope_cal, abs_amp_cal, TT_phs_cal, gen_amp_cal and gen_phs_cal flags.
    To learn more about these calibration steps, read the doc-string of the abscal_run() function
    in abscal.py, and the docstring of the AbsCal() class in abscal.py.
    """
    a = argparse.ArgumentParser(description="command-line drive script for hera_cal.abscal module")
    a.add_argument("--data_file", type=str, help="file path of data to-be-calibrated.", required=True)
    a.add_argument("--model_files", type=str, nargs='*', help="list of data-overlapping miriad files for visibility model.", required=True)
    a.add_argument("--filetype", type=str, default='uvh5', help="Filetype of input filepaths.")
    a.add_argument("--polarizations", type=str, nargs='*', default=None, help="Polarizations to load from data_file and model_files. Default is all.")
    a.add_argument("--input_cal", type=str, help="Path to a calfits file to apply to the data before running abscal.")
    a.add_argument("--output_calfits_fname", type=str, default=None, help="name of output calfits files.")
    a.add_argument("--outdir", type=str, default=None, help="output directory")
    a.add_argument("--overwrite", default=False, action='store_true', help="overwrite output calfits file if it exists.")
    a.add_argument("--silence", default=False, action='store_true', help="silence output from abscal while running.")
    a.add_argument("--data_is_omni_solution", default=False, action='store_true', help='assume input data file is an omnical visibility solution')
    a.add_argument("--all_antenna_gains", default=False, action='store_true', help='if True, use full antenna list in data file to make gains')
    a.add_argument("--delay_cal", default=False, action='store_true', help='perform antenna delay calibration')
    a.add_argument("--avg_phs_cal", default=False, action='store_true', help='perform antenna avg phase calibration')
    a.add_argument("--avg_dly_slope_cal", default=False, action='store_true', help="Perform delay slope calibration and average solution across time before applying gains.")
    a.add_argument("--delay_slope_cal", default=False, action='store_true', help='perform delay slope calibration')
    a.add_argument("--phase_slope_cal", default=False, action='store_true', help='perform frequency-indepdendent phase slope calibration')
    a.add_argument("--abs_amp_cal", default=False, action='store_true', help='perform absolute amplitude calibration')
    a.add_argument("--TT_phs_cal", default=False, action='store_true', help='perform Tip-Tilt phase slope calibration')
    a.add_argument("--phs_max_iter", type=int, default=100, help="maximum number of iterations of phase_slope_cal or TT_phs_cal allowed")
    a.add_argument("--phs_conv_crit", type=float, default=1e-6, help="convergence criterion in Delta g / g for stopping iterative phase_slope_cal or TT_phs_cal")
    a.add_argument("--gen_amp_cal", default=False, action='store_true', help='perform general antenna amplitude bandpass calibration')
    a.add_argument("--gen_phs_cal", default=False, action='store_true', help='perform general antenna phase bandpass calibration')
    a.add_argument("--max_dlst", default=0.005, type=float, help="maximum allowed LST difference in model rephasing, otherwies model is flagged.")
    a.add_argument("--refant", default=None, type=int, help="antenna number integer to use as reference antenna.")
    a.add_argument("--min_bl_cut", default=None, type=float, help="cut visibilities w/ baseline length smaller than min_bl_cut [meters].")
    a.add_argument("--max_bl_cut", default=None, type=float, help="cut visibilities w/ baseline length larger than max_bl_cut [meters].")
    a.add_argument("--bl_taper_fwhm", default=None, type=float, help="enact gaussian weight tapering based on baseline length [meters] with specified FWHM.")
    a.add_argument("--edge_cut", default=0, type=int, help="number of channels to flag on each band-edge in delay and global phase solvers.")
    a.add_argument("--solar_horizon", default=90.0, type=float, help="Solar altitude flagging threshold [degrees]. The data are flagged when the Sun is above this altitude.")
    a.add_argument("--antflag_thresh", default=0.0, type=float, help="fraction of flagged visibilities per antenna needed to flag the antenna gain per time and frequency.")
    return a


def omni_abscal_arg_parser():
    """
    argparser specifically for abscal on omnni-calibrated data. The calibration steps exposed to the user
    include: delay_slope_cal, phase_slope_cal, abs_amp_cal and TT_phs_cal. To learn more about these steps, read the
    doc-string of the abscal_run() function in abscal.py, and the docstring of the AbsCal() class in abscal.py.
    """
    a = argparse.ArgumentParser(description="command-line drive script for hera_cal.abscal module")
    a.add_argument("--data_file", type=str, help="file path of data to-be-calibrated.", required=True)
    a.add_argument("--model_files", type=str, nargs='*', help="list of data-overlapping miriad files for visibility model.", required=True)
    a.add_argument("--filetype", type=str, default='uvh5', help="Filetype of input filepaths.")
    a.add_argument("--polarizations", type=str, nargs='*', default=None, help="Polarizations to load from data_file and model_files. Default is all.")
    a.add_argument("--input_cal", type=str, help="Path to a calfits file to apply to the data before running abscal.")
    a.add_argument("--output_calfits_fname", type=str, default=None, help="name of output calfits files.")
    a.add_argument("--outdir", type=str, default=None, help="output directory")
    a.add_argument("--overwrite", default=False, action='store_true', help="overwrite output calfits file if it exists.")
    a.add_argument("--silence", default=False, action='store_true', help="silence output from abscal while running.")
    a.add_argument("--data_is_omni_solution", default=False, action='store_true', help='assume input data file is an omnical visibility solution (still beta testing optimal weighting)')
    a.add_argument("--cal_shared_antennas_only", default=False, action='store_true', help='if True, only calibrate antennas present in both data and model.')
    a.add_argument("--avg_dly_slope_cal", default=False, action='store_true', help="Perform delay slope calibration and average solution across time before applying gains.")
    a.add_argument("--delay_slope_cal", default=False, action='store_true', help='perform delay slope calibration')
    a.add_argument("--phase_slope_cal", default=False, action='store_true', help='perform frequency-indepdendent phase slope calibration')
    a.add_argument("--abs_amp_cal", default=False, action='store_true', help='perform absolute amplitude calibration')
    a.add_argument("--TT_phs_cal", default=False, action='store_true', help='perform Tip-Tilt phase slope calibration')
    a.add_argument("--phs_max_iter", type=int, default=100, help="maximum number of iterations of phase_slope_cal or TT_phs_cal allowed")
    a.add_argument("--phs_conv_crit", type=float, default=1e-6, help="convergence criterion in Delta g / g for stopping iterative phase_slope_cal or TT_phs_cal")
    a.add_argument("--max_dlst", default=0.005, type=float, help="maximum allowed LST difference in model rephasing, otherwies model is flagged.")
    a.add_argument("--refant", default=None, type=int, help="antenna number integer to use as reference antenna.")
    a.add_argument("--min_bl_cut", default=None, type=float, help="cut visibilities w/ baseline length smaller than min_bl_cut [meters].")
    a.add_argument("--max_bl_cut", default=None, type=float, help="cut visibilities w/ baseline length larger than max_bl_cut [meters].")
    a.add_argument("--bl_taper_fwhm", default=None, type=float, help="enact gaussian weight tapering based on baseline length [meters] with specified FWHM.")
    a.add_argument("--edge_cut", default=0, type=int, help="number of channels to flag on each band-edge in delay and global phase solvers.")
    a.add_argument("--solar_horizon", default=90.0, type=float, help="Solar altitude flagging threshold [degrees]. The data are flagged when the Sun is above this altitude.")
    a.add_argument("--antflag_thresh", default=0.0, type=float, help="fraction of flagged visibilities per antenna needed to flag the antenna gain per time and frequency.")
    return a


def abscal_run(data_file, model_files, filetype='uvh5', refant=None, input_cal=None, verbose=True, overwrite=False, write_calfits=True,
               min_bl_cut=None, max_bl_cut=None, bl_taper_fwhm=None, output_calfits_fname=None, outdir=None,
               match_red_bls=False, tol=1.0, reweight=False, rephase_model=True, all_antenna_gains=False, edge_cut=0,
               delay_cal=False, avg_phs_cal=False, avg_dly_slope_cal=False, delay_slope_cal=False, phase_slope_cal=False, abs_amp_cal=False,
               TT_phs_cal=False, phs_max_iter=100, phs_conv_crit=1e-6, gen_amp_cal=False, gen_phs_cal=False,
               latitude=-30.72152, max_dlst=0.005, solar_horizon=90.0, antflag_thresh=0.2, history='', **read_kwargs):
    """
    Run abscal on a set of time-contiguous data files, using time-contiguous model files that cover
    the data_files across LST.

    Parameters that control calibration steps are:

    delay_cal -> avg_phs_cal -> delay_slope_cal -> abs_amp_cal
    -> TT_phs_cal -> gen_amp_cal -> gen_phs_cal

    which are run in that order if any of these parameters are set to True. Calibration steps are
    run and then directly applied to the data before proceeding to the next calibration step. To
    learn more about these steps, see the docstring of the following functions in abscal_funcs.py:

    delay_cal : delay_lincal()
    avg_phs_cal : phs_logcal(avg=True)
    avg_dly_slope_cal : delay_slope_lincal(time_avg=True)
    delay_slope_cal : delay_slope_lincal()
    abs_amp_cal : abs_amp_logcal()
    TT_phs_cal : TT_phs_logcal()
    gen_amp_cal : amp_logcal()
    gen_phs_cal : phs_logcal()

    Parameters:
    -----------
    data_file : str
        path to data file containing complex visibility data
    model_files : list of strings
        paths to model files containing complex visibility data
    filetype : str
        filetype of input data and models
    input_cal : filepath to calfits, UVCal or HERACal object
        gain solutions to apply to data on-the-fly via hera_cal.apply_cal.calibrate_in_place
    refant : int
        antenna number integer to use as reference antenna.
    verbose : boolean
        if True print output to stdout
    overwrite : boolean
        if True, overwite output files
    write_calfits : boolean
        if True, write out gains as calfits file
    output_calfits_fname : str
        filename (not full path) of output calfits file
    outdir : str
        path to output directory
    min_bl_cut : float
        minimum baseline separation [meters] to keep in data
    max_bl_cut : float
        maximum baseline separation [meters] to keep in data
    bl_taper_fwhm : float
        impose a gaussian taper on the data weights as a function of
        bl separation length, with a specified fwhm [meters]
    match_red_bls : boolean
        match unique data baselines to model baselines based on redundancy
    tol : float
        baseline match tolerance in units of baseline vectors (e.g. meters)
    reweight : boolean
        reweight unique baseline data based on redundancy
    rephase_model : boolean
        rephase nearest neighbor model pixels onto data lst grid
    all_antenna_gains : boolean
        if True, use full antenna list in data file to make gains,
        rather than just antennas present in the data. It is not possible
        to run delay_cal, avg_phs_cal, gen_phs_cal and gen_amp_cal when all_antenna_gains is True.
    delay_cal : boolean
        if True, perform delay calibration
    avg_dly_slope_cal: boolean
        if True, run delay_slope_cal with time_avg = True
    delay_slope_cal : boolean
        if True, perform delay slope calibration
    phase_slope_cal : boolean
        if True, perform perform frequency-indepdendent phase slope calibration
    avg_phs_cal : boolean
        if True, perform average phase calibration
    abs_amp_cal : boolean
        if True, perform absolute gain calibration
    TT_phs_cal : boolean
        if True, perform iterative Tip-Tilt phase calibration
    phs_max_iter : int
        maximum number of iterations of phase_slope_cal or TT_phs_cal allowed
    phs_conv_crit : float
        convergence criterion in Delta g / g for stopping iterative phase_slope_cal or TT_phs_cal
    gen_amp_cal : boolean
        if True, perform general amplitude bandpass calibration
    gen_phs_cal : boolean
        if True, perform general phase bandpass calibration
    latitude : float
        latitude of array in degrees North
    max_dlst : float
        maximum allowed LST difference in model rephasing, otherwies model is flagged.
    edge_cut : int
        number of channels to exclude at each band edge in delay and global phase solvers
    solar_horizon : float
        Solar altitude flagging threshold [degrees]
        When the Sun is above this altitude in the data or the model, the data are flagged.
    antflag_thresh : float
        fraction of flagged visibilities per antenna needed to flag the
        antenna gain per time and frequency
    history : str
        history string to insert into output calfits files
    read_kwargs : dictionary
        keyword arguments to pass to HERAData read for datafiles and modelfiles

    Returns: AC, gain_dict, flag_dict
    --------
    AC : AbsCal object
    gain_dict : dictionary
        ant-jonespol keys with complex ndarray values
    flag_dict : dictionary
        ant-jonespol keys with bool ndarray values
    """
    # only load model files needed to create LST overlap w/ data file
    # and reject data files that have no LST overlap w/ any of model files
    model_files = sorted(set(match_times(data_file, model_files, filetype=filetype)))

    # check length of model files
    nomodelfiles = False
    if len(model_files) == 0:
        echo("no model files overlap with data files in LST", verbose=verbose)
        nomodelfiles = True

    # load model files
    if not nomodelfiles:
        echo("loading model file(s)", type=1, verbose=verbose)
        (model, model_flags, model_antpos, model_ants, model_freqs, model_times, model_lsts,
            model_pols) = io.load_vis(model_files, pop_autos=True, return_meta=True, filetype=filetype, **read_kwargs)
        antpos = model_antpos
        model_lsts[model_lsts < model_lsts[0]] += 2 * np.pi

    # check output filepath
    if write_calfits:
        # configure filename
        if output_calfits_fname is None:
            output_calfits_fname = os.path.basename(data_file) + '.abscal.calfits'
        if outdir is None:
            outdir = os.path.dirname(data_file)
        output_calfits_path = os.path.join(outdir, output_calfits_fname)

        # check path
        if os.path.exists(output_calfits_path) and not overwrite:
            raise IOError("{} exists, not overwriting".format(output_calfits_path))

    # load data and configure weights
    echo("loading {}".format(data_file), type=1, verbose=verbose)
    (data, data_flags, data_antpos, data_ants, data_freqs, data_times, data_lsts,
        data_pols) = io.load_vis(data_file, pop_autos=True, return_meta=True, pick_data_ants=False, filetype=filetype, **read_kwargs)
    bls = odict(list(map(lambda k: (k, data_antpos[k[0]] - data_antpos[k[1]]), data.keys())))
    Ntimes = len(data_times)
    Nfreqs = len(data_freqs)
    data_lsts[data_lsts < data_lsts[0]] += 2 * np.pi

    # get data ants
    total_data_antpos = copy.deepcopy(data_antpos)
    data_ants = np.unique(list(map(lambda k: k[:2], data.keys())))
    data_antpos = odict(list(map(lambda k: (k, data_antpos[k]), data_ants)))

    # ensure nomodelfiles is False
    if not nomodelfiles:
        # match redundant baselines
        if match_red_bls:
            data = match_red_baselines(data, data_antpos, model, model_antpos, tol=tol, verbose=verbose)
            antpos = model_antpos

        # solar flag data and model
        flag_utils.solar_flag(data_flags, times=data_times, flag_alt=solar_horizon, inplace=True)
        flag_utils.solar_flag(model_flags, times=model_times, flag_alt=solar_horizon, inplace=True)

        # rephase model to match data lst grid
        if rephase_model:
            model_bls = {k: model_antpos[k[0]] - model_antpos[k[1]] for k in model.keys()}
            rephase_vis(model, model_lsts, data_lsts, model_bls, data_freqs, inplace=True,
                        flags=model_flags, latitude=latitude, max_dlst=max_dlst)
            model_times = data_times
        new_model = model

        # update data flags w/ model flags
        for k in model_flags.keys():
            if k in data_flags:
                data_flags[k] += model_flags[k]

        # get wgts
        wgts = DataContainer(odict(list(map(lambda k: (k, (~data_flags[k]).astype(np.float)), data_flags.keys()))))

        # reweight according to redundancy
        if reweight:
            wgts = mirror_data_to_red_bls(wgts, model_antpos, tol=tol, weights=True)

        # instantiate class
        AC = AbsCal(new_model, data, wgts=wgts, refant=refant, antpos=antpos, freqs=data_freqs,
                    min_bl_cut=min_bl_cut, max_bl_cut=min_bl_cut, bl_taper_fwhm=bl_taper_fwhm, input_cal=input_cal)
        refant = AC.refant

        # center total_data_antpos w/ refant
        total_data_antpos = odict(list(map(lambda k: (k, total_data_antpos[k] - total_data_antpos[refant]), total_data_antpos.keys())))

        # construct total_gain_keys
        total_gain_keys = flatten(list(map(lambda p: list(map(lambda k: (k, p), total_data_antpos.keys())), AC.gain_pols)))

        # construct antenna flag_dict based purely on data flags
        gain_flag_dict = flag_utils.synthesize_ant_flags(data_flags, threshold=antflag_thresh)
        for k in total_gain_keys:
            if k not in gain_flag_dict:
                gain_flag_dict[k] = np.ones((Ntimes, Nfreqs), np.bool)

        # initialize empty gain_list
        merged_gains = []

        # perform various calibration routines
        if delay_cal:
            if all_antenna_gains:
                raise ValueError("can't run delay_cal when all_antenna_gains is True")
            AC.delay_lincal(verbose=verbose, time_avg=False, edge_cut=edge_cut)
            result_gains = merge_gains((AC.ant_dly_gain, AC.ant_dly_phi_gain))
            cal_flags = odict(list(map(lambda k: (k, np.zeros_like(result_gains[k], np.bool)), result_gains.keys())))
            apply_cal.calibrate_in_place(AC.data, result_gains, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
            merged_gains.append(AC.ant_dly_gain)
            merged_gains.append(AC.ant_dly_phi_gain)
            merged_gains = [merge_gains(merged_gains)]

        if avg_phs_cal:
            if not delay_cal:
                echo("it is recommended to run a delay_cal before avg_phs_cal", verbose=verbose)
            if all_antenna_gains:
                raise ValueError("can't run avg_phs_cal when all_antenna_gains is True")
            AC.phs_logcal(avg=True, verbose=verbose)
            cal_flags = odict(list(map(lambda k: (k, np.zeros_like(AC.ant_phi_gain[k], np.bool)), AC.ant_phi_gain.keys())))
            apply_cal.calibrate_in_place(AC.data, AC.ant_phi_gain, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
            merged_gains.append(AC.ant_phi_gain)
            merged_gains = [merge_gains(merged_gains)]

        if avg_dly_slope_cal:
            AC.delay_slope_lincal(verbose=verbose, time_avg=True, edge_cut=edge_cut)
            cal_flags = odict(list(map(lambda k: (k, np.zeros_like(AC.dly_slope_gain[k], np.bool)), AC.dly_slope_gain.keys())))
            apply_cal.calibrate_in_place(AC.data, AC.dly_slope_gain, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
            if all_antenna_gains:
                merged_gains.append(AC.custom_dly_slope_gain(total_gain_keys, total_data_antpos))
            else:
                merged_gains.append(AC.dly_slope_gain)
            merged_gains = [merge_gains(merged_gains)]

        if delay_slope_cal:
            AC.delay_slope_lincal(verbose=verbose, time_avg=False, edge_cut=edge_cut)
            cal_flags = odict(list(map(lambda k: (k, np.zeros_like(AC.dly_slope_gain[k], np.bool)), AC.dly_slope_gain.keys())))
            apply_cal.calibrate_in_place(AC.data, AC.dly_slope_gain, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
            if all_antenna_gains:
                merged_gains.append(AC.custom_dly_slope_gain(total_gain_keys, total_data_antpos))
            else:
                merged_gains.append(AC.dly_slope_gain)
            merged_gains = [merge_gains(merged_gains)]

        if phase_slope_cal:
            if not delay_slope_cal:
                echo("it is recommended to run a delay_slope_cal before phase_slope_cal", verbose=verbose)
            for i in range(phs_max_iter):
                AC.global_phase_slope_logcal(tol=tol, edge_cut=edge_cut, verbose=verbose)
                cal_flags = odict(list(map(lambda k: (k, np.zeros_like(AC.phs_slope_gain[k], np.bool)), AC.phs_slope_gain.keys())))
                apply_cal.calibrate_in_place(AC.data, AC.phs_slope_gain, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
                if all_antenna_gains:
                    merged_gains.append(AC.custom_phs_slope_gain(total_gain_keys, total_data_antpos))
                else:
                    merged_gains.append(AC.phs_slope_gain)
                # test for convergence
                if len(merged_gains) > 1:
                    gains_before = merge_gains(merged_gains[:-1])
                    gains_after = merge_gains(merged_gains)
                    # take L2 norm over antennas and times
                    gains_norm = np.linalg.norm([gains_after[k] for k in gains_after.keys()], axis=(0, 1))
                    delta_gains_norm = np.linalg.norm([gains_after[k] - gains_before[k] for k in gains_after.keys()], axis=(0, 1))
                    # take median over frequency to avoid the effect of band edges and RFI
                    echo("phase_slope_cal convergence criterion: " + str(np.median(delta_gains_norm / gains_norm)), verbose=verbose)
                    if np.median(delta_gains_norm / gains_norm) < phs_conv_crit:
                        break
                merged_gains = [merge_gains(merged_gains)]

        if abs_amp_cal:
            AC.abs_amp_logcal(verbose=verbose)
            cal_flags = odict(list(map(lambda k: (k, np.zeros_like(AC.abs_eta_gain[k], np.bool)), AC.abs_eta_gain.keys())))
            apply_cal.calibrate_in_place(AC.data, AC.abs_eta_gain, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
            if all_antenna_gains:
                merged_gains.append(AC.custom_abs_eta_gain(total_gain_keys))
            else:
                merged_gains.append(AC.abs_eta_gain)
            merged_gains = [merge_gains(merged_gains)]

        if TT_phs_cal:
            if not (delay_slope_cal and phase_slope_cal):
                echo("it is recommended to run a delay_slope_cal and a phase_slope_cal before TT_phs_cal", verbose=verbose)
            for i in range(phs_max_iter):
                AC.TT_phs_logcal(verbose=verbose)
                cal_flags = odict(list(map(lambda k: (k, np.zeros_like(AC.TT_Phi_gain[k], np.bool)), AC.TT_Phi_gain.keys())))
                apply_cal.calibrate_in_place(AC.data, AC.TT_Phi_gain, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
                if all_antenna_gains:
                    merged_gains.append(AC.custom_TT_Phi_gain(total_gain_keys, total_data_antpos))
                    merged_gains.append(AC.custom_abs_psi_gain(total_gain_keys))
                else:
                    merged_gains.append(AC.abs_psi_gain)
                    merged_gains.append(AC.TT_Phi_gain)
                # test for convergence
                if len(merged_gains) > 2:
                    gains_before = merge_gains(merged_gains[:-2])
                    gains_after = merge_gains(merged_gains)
                    # take L2 norm over antennas and times
                    gains_norm = np.linalg.norm([gains_after[k] for k in gains_after.keys()], axis=(0, 1))
                    delta_gains_norm = np.linalg.norm([gains_after[k] - gains_before[k] for k in gains_after.keys()], axis=(0, 1))
                    # take median over frequency to avoid the effect of band edges and RFI
                    echo("TT_phs_cal convergence criterion: " + str(np.median(delta_gains_norm / gains_norm)), verbose=verbose)
                    if np.median(delta_gains_norm / gains_norm) < phs_conv_crit:
                        break
                merged_gains = [merge_gains(merged_gains)]

        if gen_amp_cal:
            if all_antenna_gains:
                raise ValueError("can't run gen_amp_cal when all_antenna_gains is True")
            AC.amp_logcal(verbose=verbose)
            cal_flags = odict(list(map(lambda k: (k, np.zeros_like(AC.ant_eta_gain[k], np.bool)), AC.ant_eta_gain.keys())))
            apply_cal.calibrate_in_place(AC.data, AC.ant_eta_gain, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
            merged_gains.append(AC.ant_eta_gain)
            merged_gains = [merge_gains(merged_gains)]

        if gen_phs_cal:
            if not (delay_cal or delay_slope_cal):
                echo("it is recommended to run a delay_cal or delay_slope_cal before gen_phs_cal", verbose=verbose)
            if all_antenna_gains:
                raise ValueError("can't run gen_phs_cal when all_antenna_gains is True")
            AC.phs_logcal(verbose=verbose)
            cal_flags = odict(list(map(lambda k: (k, np.zeros_like(AC.ant_phi_gain[k], np.bool)), AC.ant_phi_gain.keys())))
            apply_cal.calibrate_in_place(AC.data, AC.ant_phi_gain, AC.wgts, cal_flags, gain_convention='divide', flags_are_wgts=True)
            merged_gains.append(AC.ant_phi_gain)
            merged_gains = [merge_gains(merged_gains)]

        # collate gains
        if len(merged_gains) == 0:
            raise ValueError("abscal_run executed without any calibration arguments set to True")
        gain_dict = merge_gains(merged_gains)
        flag_dict = odict(list(map(lambda k: (k, np.zeros((Ntimes, Nfreqs), np.bool)), gain_dict.keys())))
        gain_pols = AC.gain_pols
        gain_keys = gain_dict.keys()

        # join gain_flag_dict with flag_dict
        for k in flag_dict:
            if k in gain_flag_dict:
                flag_dict[k] += gain_flag_dict[k]

    # make blank gains if no modelfiles
    else:
        AC = None
        gain_pols = set(flatten(list(map(utils.split_pol, data_pols))))
        gain_keys = flatten(list(map(lambda p: list(map(lambda a: (a, p), data_ants)), gain_pols)))
        gain_dict = odict(list(map(lambda k: (k, np.ones((Ntimes, Nfreqs), np.complex)), gain_keys)))
        flag_dict = odict(list(map(lambda k: (k, np.ones((Ntimes, Nfreqs), np.bool)), gain_keys)))
        if refant is None:
            refant = gain_keys[0][0]

    # make extra calfits metadata
    total_qual = odict(list(map(lambda p: (p, np.ones((Ntimes, Nfreqs), np.float)), gain_pols)))
    quals = odict(list(map(lambda k: (k, np.ones((Ntimes, Nfreqs), np.float)), gain_keys)))

    # ensure reference antenna phase has been projected out (i.e. set to zero)
    for p in gain_pols:
        refant_phasor = gain_dict[(refant, p)] / np.abs(gain_dict[(refant, p)])
        for k in gain_dict.keys():
            if p in k:
                gain_dict[k] /= refant_phasor

    if write_calfits:
        # recalculate chi^2 by comparing abscal model to data
        if not nomodelfiles:
            total_qual, _, quals, _ = utils.chisq(AC.data, AC.model, AC.wgts,
                                                  gain_flags=flag_dict, split_by_antpol=True)
        io.write_cal(output_calfits_path, gain_dict, data_freqs, data_times,
                     flags=flag_dict, quality=quals, total_qual=total_qual, return_uvc=False, 
                     overwrite=overwrite, history=version.history_string(history))

    return AC, gain_dict, flag_dict


def cut_bls(datacontainer, bls=None, min_bl_cut=None, max_bl_cut=None, inplace=False):
    """
    Cut visibility data based on min and max baseline length.

    Parameters
    ----------
    datacontainer : DataContainer object to perform baseline cut on

    bls : dictionary, holding baseline position vectors.
        keys are antenna-pair tuples and values are baseline vectors in meters.
        If bls is None, will look for antpos attr in datacontainer.

    min_bl_cut : float, minimum baseline separation [meters] to keep in data

    max_bl_cut : float, maximum baseline separation [meters] to keep in data

    inplace : bool, if True edit data in input object, else make a copy.

    Output
    ------
    datacontainer : DataContainer object with bl cut enacted
    """
    if not inplace:
        datacontainer = copy.deepcopy(datacontainer)
    if min_bl_cut is None:
        min_bl_cut = 0.0
    if max_bl_cut is None:
        max_bl_cut = 1e10
    if bls is None:
        # look for antpos in dc
        if not hasattr(datacontainer, 'antpos'):
            raise ValueError("If bls is not fed, datacontainer must have antpos attribute.")
        bls = odict()
        ap = datacontainer.antpos
        for bl in datacontainer.keys():
            if bl[0] not in ap or bl[1] not in ap:
                continue
            bls[bl] = ap[bl[1]] - ap[bl[0]]
    for k in list(datacontainer.keys()):
        bl_len = np.linalg.norm(bls[k])
        if k not in bls:
            continue
        if bl_len > max_bl_cut or bl_len < min_bl_cut:
            del datacontainer[k]

    assert len(datacontainer) > 0, "no baselines were kept after baseline cut..."

    return datacontainer


def get_all_times_and_lsts(hd, solar_horizon=90.0, unwrap=True):
    '''Extract all times and lsts from a HERAData object

    Arguments:
        hd: HERAData object intialized with one ore more uvh5 file's metadata
        solar_horizon: Solar altitude threshold [degrees]. Times are not returned when the Sun is above this altitude.
        unwrap: increase all LSTs smaller than the first one by 2pi to avoid phase wrapping

    Returns:
        all_times: list of times in JD in the file or files
        all_lsts: LSTs (in radians) corresponding to all_times
    '''
    all_times = hd.times
    all_lsts = hd.lsts
    if len(hd.filepaths) > 1:  # in this case, it's a dictionary
        all_times = np.unique(all_times.values())
        all_lsts = np.ravel(all_lsts.values())[np.argsort(all_times)]  
    if unwrap:  # avoid phase wraps 
        all_lsts[all_lsts < all_lsts[0]] += 2 * np.pi
        
    # remove times when sun was too high
    lat, lon, alt = hd.telescope_location_lat_lon_alt_degrees
    solar_alts = utils.get_sun_alt(all_times, latitude=lat, longitude=lon)
    solar_flagged = solar_alts > solar_horizon
    return all_times[~solar_flagged], all_lsts[~solar_flagged]


def get_d2m_time_map(data_times, data_lsts, model_times, model_lsts):
    '''Generate a dictionary that maps data times to model times via shared LSTs.

    Arguments:
        data_times: list of times in the data (in JD)
        data_lsts: list of corresponding LSTs (in radians)
        model_times: list of times in the mdoel (in JD)
        model_lsts: list of corresponing LSTs (in radians)

    Returns:
        d2m_time_map: dictionary uniqely mapping times in the data to times in the model 
            that are closest in LST. Each model time maps to at most one data time and 
            each model time maps to at most one data time. Data times without corresponding
            model times map to None.
    '''
    # first produce a map of indices using the LSTs
    m2d_ind_map = {}  
    for dind, dlst in enumerate(data_lsts):
        nearest_mind = np.argmin(np.abs(model_lsts - dlst))
        if nearest_mind in m2d_ind_map:
            if np.abs(model_lsts[nearest_mind] < data_lsts[m2d_ind_map[nearest_mind]]):
                m2d_ind_map[nearest_mind] = dind
        else:
            m2d_ind_map[nearest_mind] = dind

    # now use those indicies to produce a map of times
    d2m_time_map = {time: None for time in data_times}
    for mind, dind in m2d_ind_map.items():
        d2m_time_map[data_times[dind]] = model_times[mind]
    return d2m_time_map


def abscal_step(gains_to_update, AC, AC_func, AC_kwargs, gain_funcs, gain_args_list, gain_flags, 
                gain_convention='divide', max_iter=1, phs_conv_crit=1e-6, verbose=True):
    '''Generalized function for performing an abscal step (e.g. abs_amp_logcal or TT_phs_logcal).

    Arguments:
        gains_to_update: the gains produced by abscal up until this step. Updated in place.
        AC: AbsCal object containing data, model, and other metadata. AC.data is recalibrated 
            in place using the gains solved for during this step
        AC_func: function (usually a class method of AC) to call to instantiate the new gains 
            which are then accessible as class properties of AC
        AC_kwargs: dictionary of kwargs to pass into AC_func
        gain_funcs: list of functions to call to return gains after AC_func has been called
        gain_args_list: list of tuples of arguments to pass to the corresponding gain_funcs
        gain_flags: per-antenna flags to apply to AC.Data when performing recalibration
        gain_convention: either 'divide' if raw data is calibrated by dividing it by the gains
            otherwise, 'multiply'.
        max_iter: maximum number of times to run phase solvers iteratively to avoid the effect
            of phase wraps in, e.g. phase_slope_cal or TT_phs_logcal
        phs_conv_crit: convergence criterion for updates to iterative phase calibration that compares
            the updates to all 1.0s. 
        verbose: If True, will print the progress of iterative convergence
    '''
    for i in range(max_iter):
        AC_func(**AC_kwargs)
        gains_here = merge_gains([gf(*gargs) for gf, gargs in zip(gain_funcs, gain_args_list)])
        apply_cal.calibrate_in_place(AC.data, gains_here, AC.wgts, gain_flags, 
                                     gain_convention=gain_convention, flags_are_wgts=True)
        for k in gains_to_update.keys():
            gains_to_update[k] *= gains_here[k]
        if max_iter > 1:
            crit = np.median(np.linalg.norm([gains_here[k] - 1.0 for 
                                             k in gains_here.keys()], axis=(0, 1)))
            echo("phase_slope_cal convergence criterion: " + str(crit), verbose=verbose)
            if crit < phs_conv_crit:
                break


def post_redcal_abscal(model, data, flags, rc_flags, min_bl_cut=None, max_bl_cut=None, edge_cut=0, 
                       tol=1.0, gain_convention='divide', phs_max_iter=100, phs_conv_crit=1e-6, verbose=True):
    '''Performs Abscal for data that has already been redundantly calibrated.

    Arguments:
        model: DataContainer containing externally calibrated visibilities, LST-matched to the data
        data: DataContainer containing redundantly but not absolutely calibrated visibilities
        flags: DataContainer containing combined data and model flags
        rc_flags: dictionary mapping keys like (1, 'Jxx') to flag waterfalls from redundant calibration
        min_bl_cut : float, eliminate all visibilities with baseline separation lengths
            smaller than min_bl_cut. This is assumed to be in ENU coordinates with units of meters.
        max_bl_cut : float, eliminate all visibilities with baseline separation lengths
            larger than max_bl_cut. This is assumed to be in ENU coordinates with units of meters.
        edge_cut : integer number of channels to exclude at each band edge in delay and global phase solvers
        tol: float distance for baseline match tolerance in units of baseline vectors (e.g. meters)
        gain_convention: either 'divide' if raw data is calibrated by dividing it by the gains
            otherwise, 'multiply'.
        phs_max_iter: maximum number of iterations of phase_slope_cal or TT_phs_cal allowed
        phs_conv_crit: convergence criterion for updates to iterative phase calibration that compares
            the updates to all 1.0s. 

    Returns:
        abscal_delta_gains: gain dictionary mapping keys like (1, 'Jxx') to waterfalls containing 
            the updates to the gains between redcal and abscal
        AC: AbsCal object containing absolutely calibrated data, model, and other useful metadata
    '''
    abscal_delta_gains = {ant: np.ones_like(g, dtype=complex) for ant, g in rc_flags.items()}

    # instantiate Abscal object
    wgts = DataContainer({k: (~flags[k]).astype(np.float) for k in flags.keys()})
    AC = AbsCal(model, data, wgts=wgts, antpos=data.antpos, freqs=data.freqs, 
                refant=pick_reference_antenna(synthesize_ant_flags(flags))[0],
                min_bl_cut=min_bl_cut, max_bl_cut=max_bl_cut)
    AC.antpos = data.antpos

    # Global Delay Slope Calibration
    for time_avg in [True, False]:
        abscal_step(abscal_delta_gains, AC, AC.delay_slope_lincal, {'time_avg': time_avg, 'edge_cut': edge_cut},
                    [AC.custom_dly_slope_gain], [(rc_flags.keys(), data.antpos)], rc_flags,
                    gain_convention=gain_convention, verbose=verbose)

    # Global Phase Slope Calibration
    abscal_step(abscal_delta_gains, AC, AC.global_phase_slope_logcal, {'tol': tol, 'edge_cut': edge_cut},
                [AC.custom_phs_slope_gain], [(rc_flags.keys(), data.antpos)], rc_flags,
                gain_convention=gain_convention, max_iter=phs_max_iter, conv_crit=phs_conv_crit, verbose=verbose)

    # Per-Channel Absolute Amplitude Calibration
    abscal_step(abscal_delta_gains, AC, AC.abs_amp_logcal, {}, [AC.custom_abs_eta_gain], 
                [(rc_flags.keys(),)], rc_flags, gain_convention=gain_convention, verbose=verbose)

    # Per-Channel Tip-Tilt Phase Calibration
    abscal_step(abscal_delta_gains, AC, AC.TT_phs_logcal, {}, [AC.custom_TT_Phi_gain, AC.custom_abs_psi_gain], 
                [(rc_flags.keys(), data.antpos), (rc_flags.keys(),)], rc_flags,
                gain_convention=gain_convention, max_iter=phs_max_iter, conv_crit=phs_conv_crit, verbose=verbose)

    return abscal_delta_gains, AC


def post_redcal_abscal_run(data_file, redcal_file, model_files, output_replace=('.omni.', '.abs.'), nInt_to_load=-1,
                           data_solar_horizon=90, model_solar_horizon=90, min_bl_cut=1.0, max_bl_cut=None, edge_cut=0, 
                           tol=1.0, phs_max_iter=100, phs_conv_crit=1e-6, clobber=True, add_to_history='', verbose=True):
    '''Perform abscal on entire data files, picking relevant model_files from a list and doing partial data loading.
    
    Arguments:
        data_file: string path to raw uvh5 visibility file
        redcal_file: string path to calfits file that redundantly calibrates the data_file
        model_files: list of string paths to externally calibrated data. Strings must be sortable 
            to produce a chronological list in LST (wrapping over 2*pi is OK)
        output_replace: tuple of two strings to find and replace in redcal_file to produce the output calfits file
        nInt_to_load: number of integrations to load and calibrate simultaneously. Default -1 loads all integrations.
        data_solar_horizon: Solar altitude threshold [degrees]. When the sun is too high in the data, flag the integration.
        model_solar_horizon: Solar altitude threshold [degrees]. When the sun is too high in the model, flag the integration.
        min_bl_cut: minimum baseline separation [meters] to keep in data when calibrating. None or 0 means no mininum,
            which will include autocorrelations in the absolute calibration. Usually this is not desired, so the default is 1.0.
        max_bl_cut: maximum baseline separation [meters] to keep in data when calibrating. None (default) means no maximum.
        edge_cut: integer number of channels to exclude at each band edge in delay and global phase solvers
        tol: baseline match tolerance in units of baseline vectors (e.g. meters)
        phs_max_iter: integer maximum number of iterations of phase_slope_cal or TT_phs_cal allowed
        phs_conv_crit: convergence criterion for updates to iterative phase calibration that compares them to all 1.0s.
        clobber: if True, overwrites existing abscal calfits file at the output path
        add_to_history: string to add to history of output abscal file

    Returns:
        hc: HERACal object which was written to disk. Matches the input redcal_file with an updated history and:
            - gains: abscal gains for times that could be calibrated, redcal gains otherwise (but flagged)
            - flags: redcal flags, with additional flagging if the data or model are flagged (see flag_utils.synthesize_ant_flags)
            - quals: abscal chi^2 p
            - total_qual: 
    '''
    # Raise error if output calfile already exists and clobber is False
    if os.path.exists(redcal_file.replace(*output_replace)) and not clobber:
        raise IOError("{} exists, not overwriting.".format(redcal_file.replace(*output_replace)))

    # Load redcal calibration
    hc = io.HERACal(redcal_file)
    rc_gains, rc_flags, rc_quals, rc_tot_qual = hc.read()

    # Initialize full-size, totally-flagged abscal gain/flag/etc. dictionaries
    abscal_gains = copy.deepcopy(rc_gains)
    abscal_flags = {ant: np.ones_like(rf) for ant, rf in rc_flags.items()}
    abscal_chisq_per_ant = {ant: np.zeros_like(rq) for ant, rq in rc_quals.items()}
    abscal_chisq = {pol: np.zeros_like(rtq) for pol, rtq in rc_tot_qual.items()}

    # match times to narrow down model_files
    matched_model_files = sorted(set(match_times(data_file, model_files, filetype='uvh5')))
    if len(matched_model_files) > 0:
        hd = io.HERAData(data_file)
        hdm = io.HERAData(matched_model_files)
        pol_load_list = [pol for pol in hd.pols if split_pol(pol)[0] == split_pol(pol)[1]]
        
        # match integrations in model to integrations in data
        all_data_times, all_data_lsts = get_all_times_and_lsts(hd, solar_horizon=data_solar_horizon, unwrap=True)
        all_model_times, all_model_lsts = get_all_times_and_lsts(hdm, solar_horizon=model_solar_horizon, unwrap=True)
        d2m_time_map = get_d2m_time_map(all_data_times, all_data_lsts, all_model_times, all_model_lsts)
        
        # group matched time indices for partial I/O
        matched_tinds = [tind for tind, time in enumerate(hd.times) if time in d2m_time_map and d2m_time_map[time] is not None]
        if len(matched_tinds) > 0:
            tind_groups = np.array([matched_tinds])  # just load a single group
            if nInt_to_load > 0:  # split up the integrations to load nInt_to_load at a time
                tind_groups = np.split(matched_tinds, np.arange(nInt_to_load, len(matched_tinds), nInt_to_load))

            # loop over polarizations
            for pol in pol_load_list:
                echo('\n\nNow calibrating ' + pol + '-polarization...', verbose=verbose)
                # loop over groups of time indices
                for tinds in tind_groups:
                    echo('\n    Now calibrating times ' + str(hd.times[tinds[0]])
                         + ' through ' + str(hd.times[tinds[-1]]) + '...', verbose=verbose)
                    
                    # load data
                    data, flags, nsamples = hd.read(times=hd.times[tinds], polarizations=[pol])
                    if not np.all(flags.values()):
                        # load model and rephase
                        model_times_to_load = [d2m_time_map[time] for time in hd.times[tinds]]
                        model, model_flags, _ = io.partial_time_io(hdm, model_times_to_load)
                        model_bls = {bl: model.antpos[bl[0]] - model.antpos[bl[1]] for bl in model.keys()}
                        utils.lst_rephase(model, model_bls, model.freqs, data.lsts - model.lsts,
                                          lat=hdm.telescope_location_lat_lon_alt_degrees[0], inplace=True)
                        
                        # update data flags w/ model flags
                        for k in flags.keys():
                            if k in model_flags:
                                flags[k] += model_flags[k]

                        # apply calibration
                        data_ants = set([ant for bl in data.keys() for ant in split_bl(bl)])
                        rc_gains_subset = {k: rc_gains[k][tinds, :] for k in data_ants}
                        rc_flags_subset = {k: rc_flags[k][tinds, :] for k in data_ants}
                        calibrate_in_place(data, rc_gains_subset, data_flags=flags, 
                                           cal_flags=rc_flags_subset, gain_convention=hc.gain_convention)
                        
                        # run absolute calibration, copying data because it gets modified internally
                        delta_gains, AC = post_redcal_abscal(model, copy.deepcopy(data), flags, rc_flags_subset, edge_cut=edge_cut, 
                                                             tol=tol, min_bl_cut=min_bl_cut, max_bl_cut=max_bl_cut, 
                                                             gain_convention=hc.gain_convention, phs_max_iter=phs_max_iter, 
                                                             phs_conv_crit=phs_conv_crit, verbose=verbose)

                        # abscal data (AC.data is already abscaled, but data is only redcaled) and generate abscal Chi^2
                        calibrate_in_place(data, delta_gains, data_flags=flags, 
                                           cal_flags=rc_flags_subset, gain_convention=hc.gain_convention)
                        chisq_wgts = {}
                        for bl in AC.data.keys():
                            dt = (np.median(np.ediff1d(hd.times_by_bl[bl[:2]])) * 86400.)
                            noise_var = predict_noise_variance_from_autos(bl, data, dt=dt)
                            chisq_wgts[bl] = noise_var**-1 * (~flags[bl]).astype(np.float)
                        total_qual, nObs, quals, nObs_per_ant = utils.chisq(AC.data, AC.model, chisq_wgts,
                                                                            gain_flags=rc_flags_subset, split_by_antpol=True)
                    
                        # update results
                        delta_flags = synthesize_ant_flags(flags)
                        for ant in data_ants:
                            abscal_gains[ant][tinds, :] = rc_gains_subset[ant] * delta_gains[ant]
                            abscal_flags[ant][tinds, :] = rc_flags_subset[ant] + delta_flags[ant]
                            abscal_chisq_per_ant[ant][tinds, :] = quals[ant] / nObs_per_ant[ant]  # Note, not normalized for DoF
                        for antpol in total_qual.keys():
                            abscal_chisq[antpol][tinds, :] = total_qual[antpol] / nObs[antpol]  # Note, not normalized for DoF
                            
        # impose a single reference antenna on the final antenna solution
        refant = pick_reference_antenna(abscal_flags)
        refant_phasor = (abscal_gains[refant] / np.abs(abscal_gains[refant]))
        for ant in abscal_gains.keys():
            abscal_gains[ant] /= refant_phasor
    else:
        echo("No model files overlap with data files in LST. Result will be fully flagged.", verbose=verbose)

    # Save results to disk
    hc.update(gains=abscal_gains, flags=abscal_flags, quals=abscal_chisq_per_ant, total_qual=abscal_chisq)
    hc.history += version.history_string(add_to_history)
    hc.write_calfits(redcal_file.replace(*output_replace), clobber=clobber)
    return hc


def post_redcal_abscal_argparser():
    ''' Argparser for commandline operation of hera_cal.abscal.post_redcal_abscal_run() '''
    a = argparse.ArgumentParser(description="Command-line drive script for post-redcal absolute calibration using hera_cal.abscal module")
    a.add_argument("data_file", type=str, help="string path to raw uvh5 visibility file")
    a.add_argument("redcal_file", type=str, help="string path to calfits file that redundantly calibrates the data_file")
    a.add_argument("model_files", type=str, nargs='+', help="list of string paths to externally calibrated data. Strings must be sortable to produce a chronological list in LST \
                                                             (wrapping over 2*pi is OK)")
    a.add_argument("--output_replace", default=('.omni.', '.abs.'), type=str, nargs=2, help="two strings to find and replace in redcal_file to produce the output calfits file")
    a.add_argument("--nInt_to_load", default=6, type=int, help="number of integrations to load and calibrate simultaneously. -1 loads all integrations.")
    a.add_argument("--data_solar_horizon", default=90.0, type=float, help="Solar altitude threshold [degrees]. When the sun is too high in the data, flag the integration.")
    a.add_argument("--model_solar_horizon", default=90.0, type=float, help="Solar altitude threshold [degrees]. When the sun is too high in the model, flag the integration.")
    a.add_argument("--min_bl_cut", default=1.0, type=float, help="minimum baseline separation [meters] to keep in data when calibrating. None or 0 means no mininum, which will \
                                                                  include autocorrelations in the absolute calibration. Usually this is not desired, so the default is 1.0.")
    a.add_argument("--max_bl_cut", default=None, type=float, help="maximum baseline separation [meters] to keep in data when calibrating. None (default) means no maximum.")
    a.add_argument("--edge_cut", default=0, type=int, help="integer number of channels to exclude at each band edge in delay and global phase solvers")
    a.add_argument("--tol", default=1.0, type=float, help="baseline match tolerance in units of baseline vectors (e.g. meters)")
    a.add_argument("--phs_max_iter", default=100, type=int, help="integer maximum number of iterations of phase_slope_cal or TT_phs_cal allowed")
    a.add_argument("--phs_conv_crit", default=1e-6, type=float, help="convergence criterion for updates to iterative phase calibration that compares them to all 1.0s.")
    a.add_argument("--clobber", default=False, action="store_true", help="overwrites existing abscal calfits file at the output path")
    a.add_argument("--verbose", default=False, action="store_true", help="print calibration progress updates")
    args = a.parse_args()
    return args
