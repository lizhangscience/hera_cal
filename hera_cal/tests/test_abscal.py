'''Tests for abscal.py'''
import nose.tools as nt
import os
import shutil
import json
import numpy as np
import aipy
import optparse
import sys
from pyuvdata import UVCal, UVData
from pyuvdata import utils as uvutils
import hera_cal as hc
from hera_cal.data import DATA_PATH
from collections import OrderedDict as odict
import copy
from hera_cal.datacontainer import DataContainer

class Test_AbsCal_Funcs:

    def setUp(self):
        np.random.seed(0)

        # load into pyuvdata object
        self.data_file = os.path.join(DATA_PATH, "zen.2458043.12552.xx.HH.uvORA")
        self.uvd = UVData()
        self.uvd.read_miriad(self.data_file)
        self.freq_array = np.unique(self.uvd.freq_array)
        self.antpos, self.ants = self.uvd.get_ENU_antpos(center=True, pick_data_ants=True)
        self.antpos = odict(zip(self.ants, self.antpos))
        self.time_array = np.unique(self.uvd.time_array)

        # configure data into dictionaries
        data, flags = hc.abscal.UVData2AbsCalDict(self.uvd, pop_autos=True)

        # configure wgts
        wgts = copy.deepcopy(flags)
        for k in wgts.keys():
            wgts[k] = (~wgts[k]).astype(np.float)

        # configure baselines
        bls = odict([(x, self.antpos[x[0]] - self.antpos[x[1]]) for x in data.keys()])

        # make mock data
        abs_gain = 0.5
        TT_phi = np.array([-0.004, 0.006, 0])
        model = odict()
        for i, k in enumerate(data.keys()):
            model[k] = data[k] * np.exp(abs_gain + 1j*np.dot(TT_phi, bls[k]))

        # assign data
        self.data = data
        self.bls = bls
        self.model = model
        self.wgts = wgts

    def test_UVData2AbsCalDict(self):
        # test filename
        fname = os.path.join(DATA_PATH, "zen.2458043.12552.xx.HH.uvORA")
        data, flags = hc.abscal.UVData2AbsCalDict(fname, pop_autos=False)
        nt.assert_equal(data[(24, 25, 'xx')].shape, (60, 64))
        nt.assert_equal(flags[(24, 25, 'xx')].shape, (60, 64))
        nt.assert_equal((24, 24, 'xx') in data, True)
        data, flags = hc.abscal.UVData2AbsCalDict([fname])
        nt.assert_equal(data[(24, 25, 'xx')].shape, (60, 64))

        # test pop autos
        data, flags = hc.abscal.UVData2AbsCalDict(fname, pop_autos=True)
        nt.assert_equal((24, 24, 'xx') in data, False)

        # test pol select
        data, flags = hc.abscal.UVData2AbsCalDict(fname, pop_autos=False, pol_select=['xx'])
        nt.assert_equal(data[(24, 25, 'xx')].shape, (60, 64))

        # test uvd object
        uvd = UVData()
        uvd.read_miriad(fname)
        data, flags = hc.abscal.UVData2AbsCalDict(uvd)
        nt.assert_equal(data[(24, 25, 'xx')].shape, (60, 64))
        data, flags = hc.abscal.UVData2AbsCalDict([uvd])
        nt.assert_equal(data[(24, 25, 'xx')].shape, (60, 64))

        # test multiple
        fname2 = os.path.join(DATA_PATH, "zen.2458043.13298.xx.HH.uvORA")
        data, flags = hc.abscal.UVData2AbsCalDict([fname, fname2])
        nt.assert_equal(data[(24, 25, 'xx')].shape, (120, 64))
        nt.assert_equal(flags[(24, 25, 'xx')].shape, (120, 64))

        # test w/ meta
        d, f, ap, a, f, t, l, p = hc.abscal.UVData2AbsCalDict([fname, fname2], return_meta=True)
        nt.assert_equal(len(ap[24]), 3)
        nt.assert_equal(len(f), len(self.freq_array))

        # test uvfits
        fname = os.path.join(DATA_PATH, 'zen.2458043.12552.xx.HH.uvA.vis.uvfits')
        d, f = hc.abscal.UVData2AbsCalDict(fname, filetype='uvfits')
        nt.assert_equal(d[(0,1,'xx')].shape, (60,64))

        # test w/ meta pick_data_ants
        d, f, ap, a, f, t, l, p = hc.abscal.UVData2AbsCalDict(fname, return_meta=True, pick_data_ants=False)
        nt.assert_equal(len(ap[24]), 3)
        nt.assert_equal(len(a), 47)
        nt.assert_equal(len(f), len(self.freq_array))

    def test_data_key_to_array_axis(self):
        m, pk = hc.abscal.data_key_to_array_axis(self.model, 2)
        nt.assert_equal(m[(24, 25)].shape, (60, 64, 1))
        nt.assert_equal('xx' in pk, True)
        # test w/ avg_dict
        m, ad, pk = hc.abscal.data_key_to_array_axis(self.model, 2, avg_dict=self.bls)
        nt.assert_equal(m[(24, 25)].shape, (60, 64, 1))
        nt.assert_equal(ad[(24, 25)].shape, (3,))
        nt.assert_equal('xx' in pk, True)

    def test_array_axis_to_data_key(self):
        m, pk = hc.abscal.data_key_to_array_axis(self.model, 2)
        m2 = hc.abscal.array_axis_to_data_key(m, 2, ['xx'])
        nt.assert_equal(m2[(24, 25, 'xx')].shape, (60, 64))
        # copy dict
        m, ad, pk = hc.abscal.data_key_to_array_axis(self.model, 2, avg_dict=self.bls)
        m2, cd = hc.abscal.array_axis_to_data_key(m, 2, ['xx'], copy_dict=ad)
        nt.assert_equal(m2[(24, 25, 'xx')].shape, (60, 64))
        nt.assert_equal(cd[(24, 25, 'xx')].shape, (3,))

    def test_interp2d(self):
        # test interpolation
        m, mf = hc.abscal.interp2d_vis(self.data, self.time_array, self.freq_array,
                                       self.time_array, self.freq_array)
        nt.assert_equal(m[(24, 25, 'xx')].shape, (60, 64))
        # downsampling
        m, mf = hc.abscal.interp2d_vis(self.data, self.time_array, self.freq_array,
                                       self.time_array[::2], self.freq_array[::2])
        nt.assert_equal(m[(24, 25, 'xx')].shape, (30, 32))

    def test_gains2calfits(self):
        cfname = os.path.join(DATA_PATH, 'ex.calfits')
        abscal_gains = np.ones((len(self.ants), 60, 1024), dtype=np.complex)
        abscal_gains = odict(map(lambda k: ((k, 'x'), abscal_gains[k]), np.arange(len(abscal_gains))))
        freq_array = np.linspace(100, 200, 1024)
        time_array = np.linspace(2450842.1, 2450842.4, 60)
        pol_array = np.array(['x'])
        if os.path.exists(cfname):
            os.remove(cfname)
        hc.abscal.gains2calfits(cfname, abscal_gains, freq_array, time_array, pol_array)
        nt.assert_equal(os.path.exists(cfname), True)
        if os.path.exists(cfname):
            os.remove(cfname)

    def test_wiener(self):
        # test smoothing
        d = hc.abscal.wiener(self.data, window=(5, 15), noise=None, medfilt=True, medfilt_kernel=(1, 13))
        nt.assert_equal(d[(24,37,'xx')].shape, (60, 64))
        nt.assert_equal(d[(24,37,'xx')].dtype, np.complex)
        # test w/ noise
        d = hc.abscal.wiener(self.data, window=(5, 15), noise=0.1, medfilt=True, medfilt_kernel=(1, 13))
        nt.assert_equal(d[(24,37,'xx')].shape, (60, 64))
        # test w/o medfilt
        d = hc.abscal.wiener(self.data, window=(5, 15), medfilt=False)
        nt.assert_equal(d[(24,37,'xx')].shape, (60, 64))
        # test as array
        d = hc.abscal.wiener(self.data[(24,37, 'xx')], window=(5, 15), medfilt=False, array=True)
        nt.assert_equal(d.shape, (60, 64))
        nt.assert_equal(d.dtype, np.complex)

    def test_Baseline(self):
        bls = map(lambda k: hc.abscal.Baseline(self.antpos[k[1]] - self.antpos[k[0]], tol=2.0), self.data.keys())
        bls_conj = map(lambda k: hc.abscal.Baseline(self.antpos[k[0]] - self.antpos[k[1]], tol=2.0), self.data.keys())
        nt.assert_equal(bls[0], bls[0])
        nt.assert_false(bls[0] == bls[1])
        nt.assert_equal(bls[0] == bls_conj[0], 'conjugated')

    def test_match_red_baselines(self):
        model = copy.deepcopy(self.data)
        model = odict([((k[0]+1, k[1]+1, k[2]), model[k]) for i,k in enumerate(model.keys())])
        model_antpos = odict([(k+1, self.antpos[k]) for i,k in enumerate(self.antpos.keys())])
        data = hc.abscal.match_red_baselines(self.data, self.antpos, model, model_antpos, tol=2.0, verbose=False)
        nt.assert_equal(len(data.keys()), 9)
        nt.assert_true((25, 38, 'xx') in data)

    def test_mirror_data_to_red_bls(self):
        # make fake data
        reds = hc.redcal.get_reds(self.antpos, pols=['xx'])
        data = DataContainer(odict(map(lambda k: (k[0], self.data[k[0]]), reds[:5])))
        # test execuation
        d = hc.abscal.mirror_data_to_red_bls(data, self.antpos)
        nt.assert_equal(len(d.keys()), 16)
        nt.assert_true((24, 25, 'xx') in d)
        # test reweighting
        w = hc.abscal.mirror_data_to_red_bls(self.wgts, self.antpos, weights=True)
        nt.assert_equal(w[(24, 25, 'xx')].dtype, np.float)
        nt.assert_almost_equal(w[(24, 25, 'xx')].max(), 16.0)

    def test_echo(self):
        hc.abscal.echo('hi', verbose=True)
        hc.abscal.echo('hi', type=1, verbose=True)

    def test_flatten(self):
        l = hc.abscal.flatten([['hi']])
        nt.assert_equal(np.array(l).ndim, 1)

    def test_avg_data_across_red_bls(self):
        data, flags, antpos, ants, freqs, times, lsts, pols = hc.abscal.UVData2AbsCalDict(self.data_file, return_meta=True)
        rd, rf, rk = hc.abscal.avg_data_across_red_bls(data, antpos, flags=self.wgts, tol=2.0)
        rd, rf, rk = hc.abscal.avg_data_across_red_bls(data, antpos, tol=2.0, median=True)
        rd, rf, rk = hc.abscal.avg_data_across_red_bls(data, antpos, tol=2.0, broadcast_flags=True)
        nt.assert_equal(len(rd.keys()), 9)
        nt.assert_equal(len(rf.keys()), 9)
        rd, rf, rk = hc.abscal.avg_data_across_red_bls(data, antpos, flags=self.wgts, tol=2.0, mirror_red_data=True)
        nt.assert_equal(len(rd.keys()), 21)
        nt.assert_equal(len(rf.keys()), 21)

    def test_avg_file_across_red_bls(self):
        rd, rf, rk = hc.abscal.avg_file_across_red_bls(self.data_file, write_miriad=False, output_data=True)
        if os.path.exists('ex'):
            shutil.rmtree('ex')
        hc.abscal.avg_file_across_red_bls(self.data_file, outdir='.', output_fname='ex', write_miriad=True, output_data=False)
        nt.assert_true(os.path.exists('ex'))
        if os.path.exists('ex'):
            shutil.rmtree('ex')


class Test_AbsCal:

    def setUp(self):
        np.random.seed(0)
        # load into pyuvdata object
        self.data_fname = os.path.join(DATA_PATH, "zen.2458043.12552.xx.HH.uvORA")
        self.model_fname = os.path.join(DATA_PATH, "zen.2458042.12552.xx.HH.uvXA")
        self.AC = hc.abscal.AbsCal(self.data_fname, self.model_fname)

        # make custom gain keys
        d, f, ap, a, f, t, l, p = hc.abscal.UVData2AbsCalDict(self.data_fname, return_meta=True, pick_data_ants=False)
        p = map(lambda p: self.AC.pol2str[p][0], p)
        self.ap = ap
        self.gk = hc.abscal.flatten(map(lambda p: map(lambda k: (k,p), a), p))

    def test_init(self):
        # init with no meta
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_almost_equal(AC.bls, None)
        # init with meta
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data, antpos=self.AC.antpos, freqs=self.AC.freqs)
        nt.assert_almost_equal(AC.bls[(24,25,'xx')][0], -14.607842046642745)
        # init with meta
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data, pol_select=['xx'])
        # test feeding file
        AC = hc.abscal.AbsCal(self.model_fname, self.data_fname)

    def test_abs_amp_logcal(self):
        # test execution and variable assignments
        self.AC.abs_amp_logcal(verbose=False)
        nt.assert_equal(self.AC.abs_eta[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.abs_eta_gain[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.abs_eta_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.abs_eta_gain_arr.shape, (7, 60, 64, 1))
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_equal(AC.abs_eta, None)
        nt.assert_equal(AC.abs_eta_arr, None)
        nt.assert_equal(AC.abs_eta_gain, None)
        nt.assert_equal(AC.abs_eta_gain_arr, None)
        # test propagation to gain_arr
        AC.abs_amp_logcal(verbose=False)
        AC._abs_eta_arr *= 0
        nt.assert_almost_equal(np.abs(AC.abs_eta_gain_arr[0,0,0,0]), 1.0)
        # test custom gain
        g = self.AC.custom_abs_eta_gain(self.gk)
        nt.assert_equal(len(g), 47)

    def test_TT_phs_logcal(self):
        # test execution
        self.AC.TT_phs_logcal(verbose=False)
        nt.assert_equal(self.AC.TT_Phi_arr.shape, (7, 2, 60, 64, 1))
        nt.assert_equal(self.AC.TT_Phi_gain_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.abs_psi_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.abs_psi_gain_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.abs_psi[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.abs_psi_gain[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.TT_Phi[(24, 'x')].shape, (2, 60, 64))
        nt.assert_equal(self.AC.TT_Phi_gain[(24, 'x')].shape, (60, 64))
        # test merge pols
        self.AC.TT_phs_logcal(verbose=False, four_pol=True)
        nt.assert_equal(self.AC.TT_Phi_arr.shape, (7, 2, 60, 64, 1))
        nt.assert_equal(self.AC.abs_psi_arr.shape, (7, 60, 64, 1))
        # test Nones
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_equal(AC.abs_psi_arr, None)
        nt.assert_equal(AC.abs_psi_gain_arr, None)
        nt.assert_equal(AC.TT_Phi_arr, None)
        nt.assert_equal(AC.TT_Phi_gain_arr, None)
        nt.assert_equal(AC.abs_psi, None)
        nt.assert_equal(AC.abs_psi_gain, None)
        nt.assert_equal(AC.TT_Phi, None)
        nt.assert_equal(AC.TT_Phi_gain, None)
        # test custom gain
        g = self.AC.custom_TT_Phi_gain(self.gk, self.ap)
        nt.assert_equal(len(g), 47)
        g = self.AC.custom_abs_psi_gain(self.gk)
        nt.assert_equal(g[(0,'x')].shape, (60, 64))

    def test_amp_logcal(self):
        self.AC.amp_logcal(verbose=False)
        nt.assert_equal(self.AC.ant_eta[(24,'x')].shape, (60, 64))
        nt.assert_equal(self.AC.ant_eta_gain[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.ant_eta_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.ant_eta_arr.dtype, np.float)
        nt.assert_equal(self.AC.ant_eta_gain_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.ant_eta_gain_arr.dtype, np.complex)
        # test Nones
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_equal(AC.ant_eta, None)
        nt.assert_equal(AC.ant_eta_gain, None)
        nt.assert_equal(AC.ant_eta_arr, None)
        nt.assert_equal(AC.ant_eta_gain_arr, None)

    def test_phs_logcal(self):
        self.AC.phs_logcal(verbose=False)
        nt.assert_equal(self.AC.ant_phi[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.ant_phi_gain[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.ant_phi_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.ant_phi_arr.dtype, np.float)
        nt.assert_equal(self.AC.ant_phi_gain_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.ant_phi_gain_arr.dtype, np.complex)
        self.AC.phs_logcal(verbose=False, avg=True)
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_equal(AC.ant_phi, None)
        nt.assert_equal(AC.ant_phi_gain, None)
        nt.assert_equal(AC.ant_phi_arr, None)
        nt.assert_equal(AC.ant_phi_gain_arr, None)

    def test_delay_lincal(self):
        # test w/o offsets
        self.AC.delay_lincal(verbose=False, kernel=(1, 3), medfilt=False, solve_offsets=False)
        nt.assert_equal(self.AC.ant_dly[(24, 'x')].shape, (60, 1))
        nt.assert_equal(self.AC.ant_dly_gain[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.ant_dly_arr.shape, (7, 60, 1, 1))
        nt.assert_equal(self.AC.ant_dly_gain_arr.shape, (7, 60, 64, 1))
        # test w/ offsets
        self.AC.delay_lincal(verbose=False, kernel=(1, 3), medfilt=False, solve_offsets=True)
        nt.assert_equal(self.AC.ant_dly_phi[(24, 'x')].shape, (60, 1))
        nt.assert_equal(self.AC.ant_dly_phi_gain[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.ant_dly_phi_arr.shape, (7, 60, 1, 1))
        nt.assert_equal(self.AC.ant_dly_phi_gain_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.ant_dly_arr.shape, (7, 60, 1, 1))
        nt.assert_equal(self.AC.ant_dly_arr.dtype, np.float)
        nt.assert_equal(self.AC.ant_dly_gain_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.ant_dly_gain_arr.dtype, np.complex)
        # test exception
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_raises(AttributeError, AC.delay_lincal)
        # test Nones
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_equal(AC.ant_dly, None)
        nt.assert_equal(AC.ant_dly_gain, None)
        nt.assert_equal(AC.ant_dly_arr, None)
        nt.assert_equal(AC.ant_dly_gain_arr, None)
        nt.assert_equal(AC.ant_dly_phi, None)
        nt.assert_equal(AC.ant_dly_phi_gain, None)
        nt.assert_equal(AC.ant_dly_phi_arr, None)
        nt.assert_equal(AC.ant_dly_phi_gain_arr, None)
        # test medfilt
        self.AC.delay_lincal(verbose=False, medfilt=False)
        self.AC.delay_lincal(verbose=False, time_avg=True)

    def test_delay_slope_lincal(self):
        # test w/o offsets
        self.AC.delay_slope_lincal(verbose=False, kernel=(1, 3), medfilt=False)
        nt.assert_equal(self.AC.dly_slope[(24, 'x')].shape, (2, 60, 1))
        nt.assert_equal(self.AC.dly_slope_gain[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.dly_slope_arr.shape, (7, 2, 60, 1, 1))
        nt.assert_equal(self.AC.dly_slope_gain_arr.shape, (7, 60, 64, 1))
        nt.assert_equal(self.AC.dly_slope_ant_dly_arr.shape, (7, 60, 1, 1))
        g = self.AC.custom_dly_slope_gain(self.gk, self.ap)
        nt.assert_equal(g[(0,'x')].shape, (60, 64))
        # test exception
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_raises(AttributeError, AC.delay_slope_lincal)
        # test Nones
        AC = hc.abscal.AbsCal(self.AC.model, self.AC.data)
        nt.assert_equal(AC.dly_slope, None)
        nt.assert_equal(AC.dly_slope_gain, None)
        nt.assert_equal(AC.dly_slope_arr, None)
        nt.assert_equal(AC.dly_slope_gain_arr, None)
        nt.assert_equal(AC.dly_slope_ant_dly_arr, None)
        # test medfilt and time_avg
        self.AC.delay_slope_lincal(verbose=False, medfilt=False)
        self.AC.delay_slope_lincal(verbose=False, time_avg=True)
        # test four pol
        self.AC.delay_slope_lincal(verbose=False, four_pol=True)
        nt.assert_equal(self.AC.dly_slope[(24, 'x')].shape, (2, 60, 1))
        nt.assert_equal(self.AC.dly_slope_gain[(24, 'x')].shape, (60, 64))
        nt.assert_equal(self.AC.dly_slope_arr.shape, (7, 2, 60, 1, 1))
        nt.assert_equal(self.AC.dly_slope_gain_arr.shape, (7, 60, 64, 1))

    def test_merge_gains(self):
        self.AC.abs_amp_logcal(verbose=False)
        self.AC.TT_phs_logcal(verbose=False)
        self.AC.delay_lincal(verbose=False)
        self.AC.phs_logcal(verbose=False)
        self.AC.amp_logcal(verbose=False)
        gains = (self.AC.abs_eta_gain, self.AC.TT_Phi_gain, self.AC.abs_psi_gain,
                 self.AC.ant_dly_gain, self.AC.ant_eta_gain, self.AC.ant_phi_gain)
        gains = hc.abscal.merge_gains(gains)
        k = (53, 'x')
        nt.assert_equal(gains[k].shape, (60, 64))
        nt.assert_equal(gains[k].dtype, np.complex)
        nt.assert_almost_equal(np.abs(gains[k][0,0]), np.abs(self.AC.abs_eta_gain[k]*self.AC.ant_eta_gain[k])[0,0])
        nt.assert_almost_equal(np.angle(gains[k][0,0]), np.angle(self.AC.TT_Phi_gain[k]*self.AC.abs_psi_gain[k]*\
                                self.AC.ant_dly_gain[k]*self.AC.ant_phi_gain[k])[0,0])

    def test_apply_gains(self):
        self.AC.abs_amp_logcal(verbose=False)
        self.AC.TT_phs_logcal(verbose=False)
        self.AC.delay_lincal(verbose=False)
        self.AC.phs_logcal(verbose=False)
        self.AC.amp_logcal(verbose=False)
        gains = (self.AC.abs_eta_gain, self.AC.TT_Phi_gain, self.AC.abs_psi_gain,
                 self.AC.ant_dly_gain, self.AC.ant_eta_gain, self.AC.ant_phi_gain)
        corr_data = hc.abscal.apply_gains(self.AC.data, gains, gain_convention='multiply')
        nt.assert_equal(corr_data[(24, 25, 'xx')].shape, (60, 64))
        nt.assert_equal(corr_data[(24, 25, 'xx')].dtype, np.complex)
        corr_data = hc.abscal.apply_gains(self.AC.data, gains, gain_convention='divide')
        nt.assert_equal(corr_data[(24, 25, 'xx')].shape, (60, 64))
        nt.assert_equal(corr_data[(24, 25, 'xx')].dtype, np.complex)

    def test_fill_dict_nans(self):
        data = copy.deepcopy(self.AC.data)
        wgts = copy.deepcopy(self.AC.wgts)
        data[(25, 38, 'xx')][15, 20] *= np.nan
        data[(25, 38, 'xx')][20, 15] *= np.inf
        hc.abscal.fill_dict_nans(data, wgts=wgts, nan_fill=-1, inf_fill=-2)
        nt.assert_equal(data[(25, 38, 'xx')][15, 20].real, -1)
        nt.assert_equal(data[(25, 38, 'xx')][20, 15].real, -2)
        nt.assert_almost_equal(wgts[(25, 38, 'xx')][15, 20], 0)
        nt.assert_almost_equal(wgts[(25, 38, 'xx')][20, 15], 0)
        data = copy.deepcopy(self.AC.data)
        wgts = copy.deepcopy(self.AC.wgts)
        data[(25, 38, 'xx')][15, 20] *= np.nan
        data[(25, 38, 'xx')][20, 15] *= np.inf
        hc.abscal.fill_dict_nans(data[(25, 38, 'xx')], wgts=wgts[(25, 38, 'xx')], nan_fill=-1, inf_fill=-2, array=True)
        nt.assert_equal(data[(25, 38, 'xx')][15, 20].real, -1)
        nt.assert_equal(data[(25, 38, 'xx')][20, 15].real, -2)
        nt.assert_almost_equal(wgts[(25, 38, 'xx')][15, 20], 0)
        nt.assert_almost_equal(wgts[(25, 38, 'xx')][20, 15], 0)

    def test_fft_dly(self):
        # test basic execution
        k = (24, 25, 'xx')
        vis = self.AC.model[k] / self.AC.data[k]
        hc.abscal.fill_dict_nans(vis, nan_fill=0.0, inf_fill=0.0, array=True)
        df = np.median(np.diff(self.AC.freqs))
        # basic execution
        dly, offset = hc.abscal.fft_dly(vis, df=df, medfilt=False, solve_phase=False)
        nt.assert_equal(dly.shape, (60, 1))
        nt.assert_equal(offset, None)
        # median filtering
        dly, offset = hc.abscal.fft_dly(vis, df=df, medfilt=True, solve_phase=False)
        nt.assert_equal(dly.shape, (60, 1))
        nt.assert_equal(offset, None)
        # solve phase
        dly, offset = hc.abscal.fft_dly(vis, df=df, medfilt=True, solve_phase=True)
        nt.assert_equal(dly.shape, (60, 1))
        nt.assert_equal(offset.shape, (60, 1))
        # test mock data
        tau = np.array([1.5e-8]).reshape(1, -1) # 15 nanoseconds
        f = np.linspace(0, 100e6, 1024)
        df = np.median(np.diff(f))
        r = np.exp(1j*2*np.pi*f*tau)
        dly, offset = hc.abscal.fft_dly(r, df=df, medfilt=True, kernel=(1, 5))
        nt.assert_almost_equal(float(dly), 1.5e-8, delta=1e-9)

    def test_abscal_arg_parser(self):
        a = hc.abscal.abscal_arg_parser()

    def test_abscal_run(self):
        data_files = [os.path.join(DATA_PATH, "zen.2458043.12552.xx.HH.uvORA")]
        model_files = [os.path.join(DATA_PATH, "zen.2458042.12552.xx.HH.uvXA"),
                       os.path.join(DATA_PATH, "zen.2458042.13298.xx.HH.uvXA")]
        # blank run
        gains = hc.abscal.abscal_run(data_files, model_files, write_calfits=False, return_gains=True, verbose=False)
        nt.assert_equal(gains[0][(24,'x')].dtype, np.complex)
        nt.assert_equal(gains[0][(24,'x')].shape, (60, 64))
        # write calfits
        outdir = "./"
        cf_name = "ex.calfits"
        if os.path.exists(os.path.join(outdir, cf_name)):
            os.remove(os.path.join(outdir, cf_name))
        gains = hc.abscal.abscal_run(data_files, model_files, write_calfits=True, calfits_fname=cf_name, outdir=outdir,
                                    return_gains=True, verbose=False)
        nt.assert_true(os.path.exists(os.path.join(outdir, cf_name)))
        if os.path.exists(os.path.join(outdir, cf_name)):
            os.remove(os.path.join(outdir, cf_name))
        # check match_red_bls and reweight
        hc.abscal.abscal_run(data_files, model_files, write_calfits=False, verbose=False,
                                     match_red_bls=True, reweight=True)
        # check all calibration routines
        gains = hc.abscal.abscal_run(data_files, model_files, write_calfits=False, verbose=False, return_gains=True,
                            delay_cal=True, avg_phs_cal=True, abs_amp_cal=True, TT_phs_cal=True,gen_amp_cal=False, gen_phs_cal=False)
        nt.assert_equal(gains[0][(24,'x')].dtype, np.complex)
        nt.assert_equal(gains[0][(24,'x')].shape, (60, 64))
        # check exceptions
        nt.assert_raises(ValueError, hc.abscal.abscal_run, data_files, model_files, alt_gains=True,
                calfits_fname='ex.calfits', abs_amp_cal=False, TT_phs_cal=False, delay_cal=True, verbose=False)
        nt.assert_raises(ValueError, hc.abscal.abscal_run, data_files, model_files, alt_gains=True,
                calfits_fname='ex.calfits', abs_amp_cal=False, TT_phs_cal=False, gen_phs_cal=True, verbose=False)
        nt.assert_raises(ValueError, hc.abscal.abscal_run, data_files, model_files, alt_gains=True,
                calfits_fname='ex.calfits', abs_amp_cal=False, TT_phs_cal=False, gen_amp_cal=True, verbose=False)
        # check alt gains run
        hc.abscal.abscal_run(data_files, model_files, alt_gains=True, write_calfits=False)
        # test general bandpass solvers
        hc.abscal.abscal_run(data_files, model_files, TT_phs_cal=False, abs_amp_cal=False, gen_amp_cal=True, gen_phs_cal=True, write_calfits=False)

    def test_mock_data(self):
        # load into pyuvdata object
        data_file = os.path.join(DATA_PATH, "zen.2458043.12552.xx.HH.uvORA")
        data, flags, ap, a, f, t, l, p = hc.abscal.UVData2AbsCalDict(data_file, return_meta=True)
        wgts = DataContainer(odict(map(lambda k: (k, (~flags[k]).astype(np.float)), flags.keys())))
        # make mock data
        dly_slope = np.array([-1e-9, 2e-9, 0])
        model = odict()
        for i, k in enumerate(data.keys()):
            bl = np.around(ap[k[0]] - ap[k[1]], 0)
            model[k] = data[k] * np.exp(2j*np.pi*f*np.dot(dly_slope, bl))
        model = DataContainer(model)
        # setup AbsCal
        AC = hc.abscal.AbsCal(model, data, antpos=ap, wgts=wgts, freqs=f)
        # run delay_slope_cal
        AC.delay_slope_lincal(time_avg=True, verbose=False)
        # test recovery
        nt.assert_almost_equal(AC.dly_slope_arr[0,0,0,0,0], -1e-9, delta=1e-10)
        nt.assert_almost_equal(AC.dly_slope_arr[0,1,0,0,0], 2e-9, delta=1e-10)
        # make mock data
        abs_gain = 0.02
        TT_phi = np.array([1e-3, -1e-3, 0])
        model = odict()
        for i, k in enumerate(data.keys()):
            bl = np.around(ap[k[0]] - ap[k[1]], 0)
            model[k] = data[k] * np.exp(abs_gain + 1j*np.dot(TT_phi, bl))
        model = DataContainer(model)
        # setup AbsCal
        AC = hc.abscal.AbsCal(model, data, antpos=ap, wgts=wgts, freqs=f)
        # run abs_amp cal
        AC.abs_amp_logcal(verbose=False)
        # run TT_phs_logcal
        AC.TT_phs_logcal(verbose=False)
        nt.assert_almost_equal(np.median(AC.abs_eta_arr[0,:,:,0][AC.wgts[(24, 25, 'xx')].astype(np.bool)]),
                                0.01, delta=1e-3)
        nt.assert_almost_equal(np.median(AC.TT_Phi_arr[0,0,:,:,0][AC.wgts[(24, 25, 'xx')].astype(np.bool)]),
                                1e-3, delta=1e-4)
        nt.assert_almost_equal(np.median(AC.TT_Phi_arr[0,1,:,:,0][AC.wgts[(24, 25, 'xx')].astype(np.bool)]),
                                -1e-3, delta=1e-4)



