import rtpipe.parsems as pm
import rtpipe.parsecal as pc
import rtpipe.parsesdm as ps
import rtlib_cython as rtlib
import multiprocessing as mp
import multiprocessing.sharedctypes as mps
from contextlib import closing
import numpy as n
from scipy.special import erf
import scipy.stats.mstats as mstats
import casautil, os, pickle, glob, time
import logging
from functools import partial
import random
import math

# setup CASA and logging
qa = casautil.tools.quanta()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.captureWarnings(True)
logger = logging.getLogger(__name__)
#if not len(logger.handlers):
#    logger.setLevel(logging.INFO)
#    ch = logging.StreamHandler()
#    ch.setLevel(logging.INFO)
#    logger.addHandler(ch)

def pipeline(d, segments):
    """ Transient search pipeline running on single node.
    Processes one or more segments of data (in which a single bgsub, (u,v,w), etc. can be used).
    Can search completely, independently, and saves candidates.
    If segments is a list of segments, then it will parallelize read/search processes.

    Stages:
    0) Take dictionary that defines metadata and search params
    -- This defines state of pipeline, including times, uv extent, pipeline search parameters, etc.
    1) Read data
    -- Overlapping reads needed to maintain sensitivity to all DMs at all times
    2) Prepare data
    -- Reads/applies telcal/CASA solutions, flags, bg time subtraction
    3) Search using all threads
    -- Option for plug-and-play detection algorithm and multiple filters
    4) Save candidate and noise info, if requested
    """

    if type(segments) == int:
        segments = [segments]

    logger.info('Starting search of %s, scan %d, segments %s' % (d['filename'], d['scan'], str(segments)))

    # seed the pseudo-random number generator # TJWL
    random.seed()    

    # set up shared arrays to fill
    data_read_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2);  data_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2)
    u_read_mem = mps.Array(mps.ctypes.c_float, d['nbl']);  u_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    v_read_mem = mps.Array(mps.ctypes.c_float, d['nbl']);  v_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    w_read_mem = mps.Array(mps.ctypes.c_float, d['nbl']);  w_mem = mps.Array(mps.ctypes.c_float, d['nbl'])

    # need these if debugging
    data = numpyview(data_mem, 'complex64', datashape(d)) # optional
    data_read = numpyview(data_read_mem, 'complex64', datashape(d)) # optional
                
    results = {}
    # only one needed for parallel read/process. more would overwrite memory space
    with closing(mp.Pool(1, initializer=initread, initargs=(data_read_mem, u_read_mem, v_read_mem, w_read_mem, data_mem, u_mem, v_mem, w_mem))) as readpool:  

        try:
            # submit all segments to pool of 1. locking data should keep this from running away.
            for segment in segments:
                assert segment in range(d['nsegments']), 'Segment %d not in range of %d nsegments' % (segment, d['nsegments'])
                candsfile = getcandsfile(d, segment)
                if d['savecands'] and os.path.exists(candsfile):
                    logger.error('candsfile %s already exists. Ending processing...' % candsfile)
                else:
                    results[segment] = readpool.apply_async(pipeline_dataprep, (d, segment))   # no need for segment here? need to think through structure...

            # step through pool of jobs and pull data off as ready. this allows pool to continue to next segment.
            while results.keys():
                for segment in results.keys():
                    logger.debug('pipeline waiting on prep to complete for segment %d' % segment)
                    if results[segment].ready():
                        job = results.pop(segment)   # returning d is a hack here
                        d = job.get()
                    else:
                        continue
                    
                    logger.debug('pipeline got result. now waiting on data lock for %d. data_read = %s. data = %s.'
                                 % (segment, str(data_read.mean()), str(data.mean())))
                    with data_mem.get_lock():
                        logger.debug('pipeline data unlocked. starting search for %d. data_read = %s. data = %s'
                                     % (segment, str(data_read.mean()), str(data.mean())))

                        if d['domock']:
                            nints = d['readints']
                            rms = data[nints/2].real.std()
                                  / n.sqrt(d['npol']*d['nbl']*d['nchan'])
                            DMmax = max(d['dmarr'])
                            logger.debug(' Adding mock transient ...')
                            (loff, moff, i, A, DM) = make_transient(nints, rms, DMmax)
                            logger.debug(' Mock transient = %f %f %d %f %f '
                                         % (loff, moff, i, A, DM))
                            add_transient(d, data, u, v, w, loff, moff, i, A, DM)

                        cands = search(d, data_mem, u_mem, v_mem, w_mem)

                    # save candidate info
                    if d['savecands']:
                        logger.info('Saving %d candidates for segment %d...'
                                    % (len(cands), segment))
                        savecands(d, cands)

        except KeyboardInterrupt:
            logger.error('Caught Ctrl-C. Closing processing pool.')
            readpool.terminate()
            readpool.join()
            raise

def pipeline_dataprep(d, segment):
    """ Single-threaded pipeline for data prep that can be started in a pool.
    """

    logger.debug('prep starting for segment %d' % segment)

    # dataprep reads for a single segment, so d['segment'] defined here
    d['segment'] = segment

    # set up numpy arrays, as expected by dataprep functions
    data_read = numpyview(data_read_mem, 'complex64', datashape(d), raw=False); data = numpyview(data_mem, 'complex64', datashape(d), raw=False)
    u_read = numpyview(u_read_mem, 'float32', d['nbl'], raw=False); u = numpyview(u_mem, 'float32', d['nbl'], raw=False)
    v_read = numpyview(v_read_mem, 'float32', d['nbl'], raw=False); v = numpyview(v_mem, 'float32', d['nbl'], raw=False)
    w_read = numpyview(w_read_mem, 'float32', d['nbl'], raw=False); w = numpyview(w_mem, 'float32', d['nbl'], raw=False)

    ####    ####    ####    ####
    # 1) Read data
    ####    ####    ####    ####

    logger.debug('prep at data_read lock for %d. data_read = %s' % (segment, str(data_read.mean())))
    with data_read_mem.get_lock():
        logger.debug('prep entering data_read lock for %d' % segment)
        if d['dataformat'] == 'ms':   # CASA-based read
            segread = pm.readsegment(d, segment)
            data_read[:] = segread[0]
            (u_read[:], v_read[:], w_read[:]) = (segread[1][d['readints']/2], segread[2][d['readints']/2], segread[3][d['readints']/2])  # mid int good enough for segment. could extend this to save per chunk
            del segread
        elif d['dataformat'] == 'sdm':
            data_read[:] = ps.read_bdf_segment(d, segment)
            (u_read[:], v_read[:], w_read[:]) = ps.get_uvw_segment(d, segment)

        ####    ####    ####    ####
        # 2) Prepare data
        ####    ####    ####    ####

        # calibrate data
        if os.path.exists(d['gainfile']):
            try:
                radec = (); spwind = []; calname = ''  # set defaults
                if '.GN' in d['gainfile']: # if telcal file
                    if d.has_key('calname'):
                        calname = d['calname']

                    sols = pc.telcal_sol(d['gainfile'])   # parse gainfile
                else:   # if CASA table
                    if d.has_key('calradec'):
                        radec = d['calradec']  # optionally defined cal location

                    spwind = d['spw']
                    sols = pc.casa_sol(d['gainfile'], flagants=d['flagantsol'])   # parse gainfile
                    sols.parsebp(d['bpfile'])   # parse bpfile

                # if gainfile parsed ok, choose best solution for data
                sols.set_selection(d['segmenttimes'][segment].mean(), d['freq']*1e9, rtlib.calc_blarr(d), calname=calname, pols=d['pols'], radec=radec, spwind=spwind)
                sols.apply(data_read)
            except:
                logger.warning('Could not parse or apply gainfile %s.' % d['gainfile'])
                raise
        else:
            logger.info('Calibration file not found. Proceeding with no calibration applied.')

        # flag data
        if len(d['flaglist']):
            logger.info('Flagging with flaglist: %s' % d['flaglist'])
            dataflag(d, data_read)
        else:
            logger.info('No real-time flagging.')

        # mean t vis subtration
        if d['timesub'] == 'mean':
            logger.info('Subtracting mean visibility in time...')
            rtlib.meantsub(data_read, [0, d['nbl']])
        else:
            logger.info('No mean time subtraction.')

        # save noise pickle
        if d['savenoise']:
            noisepickle(d, data_read, u_read, v_read, w_read, chunk=200)

        # phase to new location if l1,m1 set and nonzero value
        try:
            if any([d['l1'], d['m1']]):
                logger.info('Rephasing data to (l, m)=(%.4f, %.4f).' % (d['l1'], d['m1']))
                rtlib.phaseshift_threaded(data_read, d, d['l1'], d['m1'], u_read, v_read)
                d['l0'] = d['l1']
                d['m0'] = d['m1']
            else:
                logger.debug('Not rephasing.')
        except KeyError:
            pass

        logger.debug('prep finished data_read mods and waiting for data lock for segment %d. data_read = %s. data = %s' % (segment, str(data_read.mean()), str(data.mean())))
        with data_mem.get_lock():
            logger.debug('prep data_read unlocked for segment %d. data_read = %s. data = %s.' % (segment, str(data_read.mean()), str(data.mean())))
            data[:] = data_read[:]
            u[:] = u_read[:]; v[:] = v_read[:]; w[:] = w_read[:]
            logger.debug('prep copied into data for segment %d. data_read = %s. data = %s.' % (segment, str(data_read.mean()), str(data.mean())))
    logger.info('All data unlocked for segment %d' % segment)

    # d now has segment keyword defined
    return d

def pipeline_reproduce(d, segment, candloc = ()):
    """ Reproduces candidates for a given candidate.
    candloc is tuple of (dmind, dtind) or (candint, dmind, dtind). 
    Former returns corrected data, latter images and phases data.
    """

    # set up shared arrays to fill
    data_reproduce_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2)
    data_read_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2)
    data_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2)
    u_read_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    u_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    v_read_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    v_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    w_read_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    w_mem = mps.Array(mps.ctypes.c_float, d['nbl'])

    # get numpy views of memory spaces
    data = numpyview(data_mem, 'complex64', datashape(d)) # optional
    data_read = numpyview(data_read_mem, 'complex64', datashape(d)) # optional
    u = numpyview(u_mem, 'float32', d['nbl'], raw=False)
    v = numpyview(v_mem, 'float32', d['nbl'], raw=False)
    w = numpyview(w_mem, 'float32', d['nbl'], raw=False)

    with closing(mp.Pool(1, initializer=initread, initargs=(data_read_mem, u_read_mem, v_read_mem, w_read_mem, data_mem, u_mem, v_mem, w_mem))) as readpool:  
        readpool.apply(pipeline_dataprep, (d, segment))

    if len(candloc) == 0:
        logger.info('Returning prepared data...')
        return data

    elif len(candloc) == 2:
        logger.info('Reproducing data...')
        dmind, dtind = candloc
        d['dmarr'] = [d['dmarr'][dmind]]
        d['dtarr'] = [d['dtarr'][dtind]]
        data = runreproduce(d, data_mem, data_reproduce_mem, u, v, w)
        return data

    elif len(candloc) == 3:  # reproduce candidate image and data
        logger.info('Reproducing candidate...')
        reproduceint, dmind, dtind = candloc
        d['dmarr'] = [d['dmarr'][dmind]]
        d['dtarr'] = [d['dtarr'][dtind]]
        im, data = runreproduce(d, data_mem, data_reproduce_mem, u, v, w, reproduceint)
        return im, data

    else:
        logger.error('reproducecand not in expected format: %s' % reproducecand)

def meantsubpool(d, data_read):
    """ Wrapper for mean visibility subtraction in time.
    Doesn't work when called from pipeline using multiprocessing pool.
    """

    logger.info('Subtracting mean visibility in time...')
    data_read = numpyview(data_read_mem, 'complex64', datashape(d))
    tsubpart = partial(rtlib.meantsub, data_read)

    blranges = [(d['nbl'] * t/d['nthread'], d['nbl']*(t+1)/d['nthread']) for t in range(d['nthread'])]
    with closing(mp.Pool(1, initializer=initreadonly, initargs=(data_read_mem,))) as tsubpool:
        tsubpool.map(tsubpart, blr)

def dataflag(d, data_read):
    """ Flagging data in single process 
    """

    for flag in d['flaglist']:
        mode, sig, conv = flag
        resultlist = []
#        with closing(mp.Pool(4, initializer=initreadonly, initargs=(data_read_mem,))) as flagpool:
        for ss in d['spw']:
            chans = n.arange(d['spw_chanr_select'][ss][0], d['spw_chanr_select'][ss][1])
            for pol in range(d['npol']):
                status = rtlib.dataflag(data_read, chans, pol, d, sig, mode, conv)
                logger.info(status)

def dataflagatom(chans, pol, d, sig, mode, conv):
    """ Wrapper function to get shared memory as numpy array into pool
    Assumes data_mem is global mps.Array
    """

    data = numpyview(data_mem, 'complex64', datashape(d))
#    data = n.ma.masked_array(data, data==0j)  # this causes massive overflagging on 14sep03 data

    return rtlib.dataflag(data, chans, pol, d, sig, mode, conv)

def search(d, data_mem, u_mem, v_mem, w_mem):
    """ Search function.
    Queues all trials with multiprocessing.
    Assumes shared memory system with single uvw grid for all images.
    """

    data = numpyview(data_mem, 'complex64', datashape(d))
    u = numpyview(u_mem, 'float32', d['nbl'])
    v = numpyview(v_mem, 'float32', d['nbl'])
    w = numpyview(w_mem, 'float32', d['nbl'])
    data_resamp_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2)
    data_resamp = numpyview(data_resamp_mem, 'complex64', datashape(d))

    logger.debug('Search of segment %d' % d['segment'])

    beamnum = 0   # not yet implemented
    cands = {}

    candsfile = getcandsfile(d)
    if d['savecands'] and os.path.exists(candsfile):
        logger.warn('candsfile %s already exists' % candsfile)
        return cands

    # make wterm kernels
    if d['searchtype'] == 'image2w':
        wres = 100
        npix = max(d['npixx_full'], d['npixy_full'])
        bls, uvkers = rtlib.genuvkernels(w, wres, npix, d['uvres'], thresh=0.05)

    # SUBMITTING THE LOOPS
    if n.any(data):
        logger.info('Searching in %d chunks with %d threads' % (d['nchunk'], d['nthread']))
        logger.info('Dedispering to max (DM, dt) of (%d, %d) ...' % (d['dmarr'][-1], d['dtarr'][-1]) )

        # open pool
        with closing(mp.Pool(d['nthread'], initializer=initresamp, initargs=(data_mem, data_resamp_mem))) as resamppool:
            for dmind in xrange(len(d['dmarr'])):
                for dtind in xrange(len(d['dtarr'])):
                    # set partial functions for pool.map
                    correctpart = partial(correct_dmdt, d, dmind, dtind)
                    image1part = partial(image1, d, u, v, w, dmind, dtind, beamnum)

                    # dedispersion in shared memory, mapped over baselines
                    logger.debug('Dedispersing for (%d,%d)' % (d['dmarr'][dmind], d['dtarr'][dtind]),)
                    blranges = [(d['nbl'] * t/d['nthread'], d['nbl']*(t+1)/d['nthread']) for t in range(d['nthread'])]
                    dedispresults = resamppool.map(correctpart, blranges)

                    # set dm- and dt-dependent int ranges for segment
                    nskip_dm = ((d['datadelay'][-1] - d['datadelay'][dmind]) / d['dtarr'][dtind]) * (d['segment'] != 0)  # nskip=0 for first segment
                    searchints = (d['readints'] - d['datadelay'][dmind]) / d['dtarr'][dtind] - nskip_dm
                    logger.info('Imaging %d ints from %d for (%d,%d)' % (searchints, nskip_dm, d['dmarr'][dmind], d['dtarr'][dtind]),)

                    # imaging in shared memory, mapped over ints
                    irange = [(nskip_dm + searchints*chunk/d['nchunk'], nskip_dm + searchints*(chunk+1)/d['nchunk']) for chunk in range(d['nchunk'])]
                    imageresults = resamppool.map(image1part, irange)

                    # COLLECTING THE RESULTS per dm/dt. Clears the way for overwriting data_resamp
                    for imageresult in imageresults:
                        for kk in imageresult.keys():
                            cands[kk] = imageresult[kk]

    else:
        logger.warn('Data for processing is zeros. Moving on...')

    logger.info('Found %d cands in scan %d segment %d of %s. ' % (len(cands), d['scan'], d['segment'], d['filename']))
    return cands

def runreproduce(d, data_mem, data_resamp_mem, u, v, w, candint=-1, twindow=30):
    """ Reproduce function, much like search.
    If no candint is given, it returns resampled data. Otherwise, returns image and rephased data.
    """

    dmind = 0; dtind = 0
    data_resamp = numpyview(data_resamp_mem, 'complex64', datashape(d))

    with closing(mp.Pool(1, initializer=initresamp, initargs=(data_mem, data_resamp_mem))) as repropool:
        # dedisperse
        logger.info('Dedispersing with DM=%.1f, dt=%d...' % (d['dmarr'][dmind], d['dtarr'][dtind]))
        repropool.apply(correct_dmdt, [d, dmind, dtind, (0,d['nbl'])])

        # set up image
        if d['searchtype'] == 'image1':
            npixx = d['npixx']
            npixy = d['npixy']
        elif d['searchtype'] == 'image2':
            npixx = d['npixx_full']
            npixy = d['npixy_full']

        if candint > -1:
            # image
            logger.info('Imaging int %d with %d %d pixels...' % (candint, npixx, npixy))
            im = repropool.apply(image1wrap, [d, u, v, w, npixx, npixy, candint/d['dtarr'][dtind]])

            snrmin = im.min()/im.std()
            snrmax = im.max()/im.std()
            logger.info('Made image with SNR min, max: %.1f, %.1f' % (snrmin, snrmax))
            if snrmax > -1*snrmin:
                l1, m1 = calc_lm(d, im, minmax='max')
            else:
                l1, m1 = calc_lm(d, im, minmax='min')

            # rephase and trim interesting ints out
            repropool.apply(move_phasecenter, [d, l1, m1, u, v])
            minint = max(candint/d['dtarr'][dtind]-twindow/2, 0)
            maxint = min(candint/d['dtarr'][dtind]+twindow/2, len(data_resamp)/d['dtarr'][dtind])

            return(im, data_resamp[minint:maxint].mean(axis=1))
        else:
            return data_resamp

def add_transient(d, data, u, v, w, l1, m1, i, s, dm=0, dt=1):
    """ Add a transient to data.
    l1, m1 are relative direction cosines (location) of transient
    added at integration i (at highest freq) with brightness s (per int/chan/bl/pol in data units)
    dm/dt are dispersion (in pc/cm3) and pulse width (in s).
    """

    ang = lambda ch: l1 * u * d['freq'][ch]/d['freq_orig'][0] + m1 * v * d['freq'][ch]/d['freq_orig'][0]
    delay = lambda ch: n.round(4.2e-3 * dm * (d['freq'][ch]**(-2) - d['freq'][-1]**(-2))/d['inttime'], 0).astype(int)

    #snr_ideal = s/(data[i].real.std()/n.sqrt(d['npol']*d['nbl']*d['nchan']))
    #logger.info('SNR of source with system brightness %.1f = %d (idealized; ok at low SNR)' % (s, int(snr_ideal)))

    for ch in range(d['nchan']):
        data[i+delay(ch):i+delay(ch)+dt, :, ch] += s * n.exp(2j*n.pi*ang(ch)[None,:,None])
#
def make_transient(nints, rms, DMmax):
    """ Produce a mock transient pulse source for the purposes of characterizing the
    detection success of the current pipeline.
    
    Assumes
    - Code to inject the transients does so by inserting at an array index 
    - Noise level at the center of the data array is characteristic of the
      noise level throughout

    Input
    nints - number of integrations
    rms   - rms noise level in visibilities(?) at mid-point of segment
    
    Returns
    loff - direction cosine offset of mock transient from phase center [radians]
    moff - direction cosine offset of mock transient from phase center [radians]
    A  - amplitude of transient [rms units]
    DM - dispersion measure of mock transient [pc/cm^3]
    dt - 'width,' but really more like offset index [integrations]
    """
    #
    #
    #
    rad_arcmin = math.pi/(180*60)

    Amin   =  6.0  # amplitude in units of the rms (calculated below)
    Amax   = 15.0  # amplitude in units of the rms (calculated below)

    rmax   = 20.0  # [arcmin]
    rmin   =  0.0  # [arcmin]
    phimin =  0.0
    phimax = 2*math.pi
    
    DMmin  = 0.0 # [pc/cm^3]
    #    
    # Amplitude of transient, done in units of the rms
    # rms is calculated assuming that noise level in the middle of the data, 
    # at index d['readints']/2, is characteristic of that throughout the data
    #
    A = random.uniform(Amin, Amax) * rms
    #
    # Position of transient, in direction cosines
    #
    r = random.uniform(rmin, rmax)
    phi = random.uniform(phimin, phimax)
    #
    loff = r*math.cos(phi) * rad_arcmin
    moff = r*math.sin(phi) * rad_arcmin
    #
    # Dispersion measure
    #
    DM = random.uniform(DMmin, DMmax)
    #
    # "Width," though measured in integration time units
    #
    i = random.uniform(0, nints)
    #
    return loff, moff, i, A, DM
#
def pipeline_lightcurve(d, l1=0, m1=0, segments=[], scan=-1):
    """ Makes lightcurve at given (l1, m1)
    l1, m1 define phase center. if not set, then image max is used.
    """

    if scan == -1: scan = d['scan']
    if segments == []: segments = range(d['nsegments'])

    d = set_pipeline(d['filename'], scan, fileroot=d['fileroot'], dmarr=[0], dtarr=[1], savenoise=False, timesub='', nologfile=True, nsegments=d['nsegments'])

    # define memory and numpy arrays
    data_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2)
    data_read_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2)
    data_resamp_mem = mps.Array(mps.ctypes.c_float, datasize(d)*2)
    u_read_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    u_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    v_read_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    v_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    w_read_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    w_mem = mps.Array(mps.ctypes.c_float, d['nbl'])
    data_read = numpyview(data_read_mem, 'complex64', datashape(d)) # optional
    u_read = numpyview(u_read_mem, 'float32', d['nbl'], raw=False)
    v_read = numpyview(v_read_mem, 'float32', d['nbl'], raw=False)
    w_read = numpyview(w_read_mem, 'float32', d['nbl'], raw=False)
    lightcurve = n.zeros(shape=(d['nints'], d['nchan'], d['npol']), dtype='complex64')

    phasecenters = []
    with closing(mp.Pool(1, initializer=initread, initargs=(data_read_mem, u_read_mem, v_read_mem, w_read_mem, data_mem, u_mem, v_mem, w_mem))) as readpool:  
        for segment in segments:
            logger.info('Reading data...')
            readpool.apply(pipeline_dataprep, (d, segment))

            # get image peak for rephasing
            if not any([l1, m1]):
                im = sample_image(d, data_read, u_read, v_read, w_read, i=-1, verbose=1, imager='xy')
                l2, m2 = calc_lm(d, im)
            else:
                l2 = l1
                m2 = m1

            logger.info('Rephasing data to (l, m)=(%.4f, %.4f).' % (l2, m2))
            rtlib.phaseshift_threaded(data_read, d, l2, m2, u_read, v_read)
            phasecenters.append( (l2,m2) )

            nskip = (24*3600*(d['segmenttimes'][segment,0] - d['starttime_mjd'])/d['inttime']).astype(int)   # insure that lc is set as what is read
            lightcurve[nskip: nskip+d['readints']] = data_read.mean(axis=1)

    return phasecenters, lightcurve

def set_pipeline(filename, scan, fileroot='', paramfile='', **kwargs):
    """ Function defines pipeline state for search. Takes data/scan as input.
    fileroot is base name for associated products (cal files, noise, cands). if blank, it is set to filename.
    paramfile is name of file that defines all pipeline parameters (python-like syntax).
    kwargs used to overload paramfile definitions.
    Many parameters take 0 as default, which auto-defines ideal parameters. 
    This definition does not yet consider memory/cpu/time limitations.
    nsegments defines how to break jobs in time. nchunk defines how many jobs are sent to nthreads.
    """

    
    workdir = os.path.dirname(os.path.abspath(filename))
    filename = filename.rstrip('/')

    # option of not writing log file (need to improve later)
    if 'silent' in kwargs.keys():
        loglevel = logging.ERROR
    else:
        loglevel = logging.INFO

    logger.setLevel(loglevel)
    
    if ('nologfile' in kwargs.keys()) or ('silent' in kwargs.keys()):
        pass
    else:
        fh = logging.FileHandler(os.path.join(workdir, 'rtpipe_%d.log' % int(round(time.time()))))
        fh.setLevel(loglevel)
        fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(fh)

    # define metadata (state) dict. chans/spw is special because it goes in to get_metadata call
    # if 'chans' in kwargs.keys(): 
    #     chans=kwargs['chans']
    # else:
    #     chans = []
    # if 'spw' in kwargs.keys(): 
    #     spw=kwargs['spw']
    # else:
    #     spw = []
    # if 'read_fdownsample' in kwargs.keys(): 
    #     rfd=kwargs['read_fdownsample']
    # else:
    #     rfd = 1
    # if 'datacol' in kwargs.keys(): 
    #     datacol=kwargs['datacol']
    # else:
    #     datacol = []

    # then get all metadata
    assert os.path.exists(filename)

    if os.path.exists(os.path.join(filename, 'Main.xml')):
        d = ps.get_metadata(filename, scan, paramfile=paramfile, **kwargs)   # can take file name or Params instance
        d['dataformat'] = 'sdm'
    else:
        d = pm.get_metadata(filename, scan, paramfile=paramfile, **kwargs)
        d['dataformat'] = 'ms'

    # define rootname for in/out cal/products
    if fileroot:
        d['fileroot'] = fileroot
    else:
        d['fileroot'] = os.path.basename(os.path.abspath(filename))

    # autodetect calibration products locally
    if not d['gainfile']:
        # first try to get CASA gain file
        try:
            filelist = glob.glob(os.path.join(d['workdir'], d['fileroot'] + '.g?'))
            filelist.sort()
            d['gainfile'] = filelist[-1]
            logger.info('Autodetected CASA gainfile %s' % d['gainfile'])

            # if CASA gainfile, look for CASA bpfile
            filelist = glob.glob(os.path.join(d['workdir'], d['fileroot'] + '.b?'))
            filelist.sort()
            d['bpfile'] = filelist[-1]
            logger.info('Autodetected CASA bpfile %s' % d['bpfile'])
        except:
            # if that fails, look for telcal file
            try:
                filelist = glob.glob(os.path.join(d['workdir'], filename + '.GN'))
                d['gainfile'] = filelist[0]
                logger.info('Autodetected telcal file %s' % d['gainfile'])
            except:
                logger.debug('Failed to autodetect CASA or telcal calibration files.')

    # supported features: snr1, immax1, l1, m1
    if d['searchtype'] == 'image1':
        d['features'] = ['snr1', 'immax1', 'l1', 'm1']   # features returned by image1
    elif d['searchtype'] == 'image1snip':
        d['features'] = ['snr1', 'immax1', 'l1', 'm1', 'im40', 'spec20']
    elif d['searchtype'] == 'image1stats':
        d['features'] = ['snr1', 'immax1', 'l1', 'm1', 'specstd', 'specskew', 'speckurtosis', 'imskew', 'imkurtosis']  # note: spec statistics are all or nothing.
    elif 'image2' in d['searchtype']:
        d['features'] = ['snr1', 'immax1', 'l1', 'm1', 'snr2', 'immax2', 'l2', 'm2']   # features returned by image1
    d['featureind'] = ['segment', 'int', 'dmind', 'dtind', 'beamnum']  # feature index. should be stable.

    # set imaging parameters to use
    if d['uvres'] == 0:
        d['uvres'] = d['uvres_full']
    else:
        urange = d['urange'][scan]*(d['freq'].max()/d['freq_orig'][0])   # uvw from get_uvw already in lambda at ch0
        vrange = d['vrange'][scan]*(d['freq'].max()/d['freq_orig'][0])
        powers = n.fromfunction(lambda i,j: 2**i*3**j, (14,10), dtype='int')   # power array for 2**i * 3**j
        rangex = n.round(d['uvoversample']*urange).astype('int')
        rangey = n.round(d['uvoversample']*vrange).astype('int')
        largerx = n.where(powers-rangex/d['uvres'] > 0, powers, powers[-1,-1])
        p2x, p3x = n.where(largerx == largerx.min())
        largery = n.where(powers-rangey/d['uvres'] > 0, powers, powers[-1,-1])
        p2y, p3y = n.where(largery == largery.min())
        d['npixx_full'] = (2**p2x * 3**p3x)[0]
        d['npixy_full'] = (2**p2y * 3**p3y)[0]

    if d['npix'] == 0:
        if d.has_key('npix_max'):   # optional 'do not exceed' image specification
            d['npixx'] = min(d['npix_max'], d['npixx_full'])
            d['npixy'] = min(d['npix_max'], d['npixy_full'])
        else:    # otherwise, go with full res
            d['npixx'] = d['npixx_full']
            d['npixy'] = d['npixy_full']
        d['npix'] = max(d['npixx'], d['npixy'])   # this used to define fringe time
    else:
        d['npixx'] = d['npix']
        d['npixy'] = d['npix']

    # define dmarr, if not already
    if len(d['dmarr']) == 0:
        if d.has_key('dm_maxloss') and d.has_key('maxdm') and d.has_key('dm_pulsewidth'):
            d['dmarr'] = calc_dmgrid(d, maxloss=d['dm_maxloss'], maxdm=d['maxdm'], dt=d['dm_pulsewidth'])
            if d['maxdm'] > 0:
                logger.info('Calculated %d dms for max sensitivity loss %.2f, maxdm %d pc/cm3, and pulse width %d ms' % (len(d['dmarr']), d['dm_maxloss'], d['maxdm'], d['dm_pulsewidth']/1000))
        else:
            d['dmarr'] = [0]
            logger.info('Can\'t calculate dm grid without dm_maxloss, maxdm, and dm_pulsewidth defined. Setting to [0].')

    # define times for data to read
    d['t_overlap'] = rtlib.calc_delay(d['freq'], d['inttime'], max(d['dmarr'])).max()*d['inttime']   # time of overlap for total dm coverage at segment boundaries
    d['datadelay'] = [rtlib.calc_delay(d['freq'], d['inttime'],dm).max() for dm in d['dmarr']]
    d['nints'] = d['nints'] - d['nskip']

    # pols
    if d.has_key('selectpol'):
        d['pols'] = [pol for pol in d['pols_orig'] if pol in d['selectpol']]
    else:
        d['pols'] = d['pols_orig']
    d['npol'] = len(d['pols'])

    # split imaging into chunks. ideally one per thread, but can modify to fit available memory
    if d['nchunk'] == 0:
        d['nchunk'] = d['nthread']

    # if nsegments is 0, then auto-define within memory limit
    if not d['nsegments']:
        fringetime = calc_fringetime(d)
        d['nsegments'] = max(1, min(d['nints'], int(d['scale_nsegments']*d['inttime']*d['nints']/(fringetime-d['t_overlap']))))  # at least 1, at most nints
        calc_segment_times(d)

        # if auto nsegment definition makes segment too large, try to scale it down to fit in memory_limit (if provided)
        if d.has_key('memory_limit'):
            while calc_memory_footprint(d, visonly=True) > d['memory_limit']:
                logger.info("For nsegments=%d, pipeline would read %d GB of visibilities and exceed limit of %d. Iterating to better memory fit." % (d['nsegments'], calc_memory_footprint(d, visonly=True), d['memory_limit']))
                d['scale_nsegments'] = d['scale_nsegments'] * (calc_memory_footprint(d, visonly=True)/float(d['memory_limit']))
                d['nsegments'] = max(1, min(d['nints'], int(d['scale_nsegments']*d['inttime']*d['nints']/(fringetime-d['t_overlap']))))  # at least 1, at most nints

                calc_segment_times(d)
    else:
        # if nsegments defined manually, then calc times
        calc_segment_times(d)

    # scaling of number of integrations beyond dt=1
    assert all(d['dtarr']), 'dtarr must be larger than 0'

    # calculate number of thermal noise candidates per segment
    nfalse = calc_nfalse(d)

    logger.info('')
    logger.info('Pipeline summary:')
    if '.GN' in d['gainfile']:
        logger.info('\t Products saved with %s. telcal calibration with %s' % (d['fileroot'], os.path.basename(d['gainfile'])))
    else:
        logger.info('\t Products saved with %s. CASA calibration files (%s, %s)' % (d['fileroot'], os.path.basename(d['gainfile']), os.path.basename(d['bpfile'])))
    logger.info('\t Using %d segment%s of %d ints (%.1f s) with overlap of %.1f s' % (d['nsegments'], "s"[not d['nsegments']-1:], d['readints'], d['t_segment'], d['t_overlap']))
    if d['t_overlap'] > d['t_segment']/3.:
        logger.info('\t\t Lots of segments needed, since Max DM sweep (%.1f s) close to segment size (%.2f s)' % (d['t_overlap'], d['t_segment']))
    logger.info('\t Downsampling in time/freq by %d/%d and skipping %d ints from start of scan.' % (d['read_tdownsample'], d['read_fdownsample'], d['nskip']))
    logger.info('\t Excluding ants %s' % (d['excludeants']))
    logger.info('\t Using pols %s' % (d['pols']))
    logger.info('')

    logger.info('\t Search with %s and threshold %.1f.' % (d['searchtype'], d['sigma_image1']))
    logger.info('\t Using %d DMs from %.1f to %.1f and dts %s.' % (len(d['dmarr']), min(d['dmarr']), max(d['dmarr']), d['dtarr']))
    logger.info('\t Using uvgrid npix=(%d,%d) and res=%d.' % (d['npixx'], d['npixy'], d['uvres']))
    logger.info('\t Expect %d thermal false positives per segment.' % nfalse)

    (vismem, immem) = calc_memory_footprint(d)
    if d.has_key('memory_limit'):    # if preference given, then test
        assert vismem < d['memory_limit'], 'Visibility reading requires %d GB, but memory limit is %d GB. Try forcing nsegments to value larger than %d.' % (vismem, d['memory_limit'], d['nsegments'])

        if vismem+immem > d['memory_limit']:
            logger.info('')
            while vismem+immem > d['memory_limit']:
                logger.info('Doubling nchunk from %d to fit in %d GB memory limit.' % (d['nchunk'], d['memory_limit']))
                d['nchunk'] = 2*d['nchunk']
                (vismem, immem) = calc_memory_footprint(d)

    logger.info('')
    logger.info('\t Visibility memory usage is %.1f GB/segment' % vismem)
    logger.info('\t Imaging in %d chunk%s using max of %.1f GB/segment' % (d['nchunk'], "s"[not d['nsegments']-1:], immem))
    logger.info('\t Grand total memory usage: %d GB/segment' % (vismem + immem))

    return d

def getcandsfile(d, segment=-1):
    """ Return name of candsfile for a given dictionary. Must have d['segment'] defined.
    """
    if d.has_key('segment'):
        return os.path.join(d['workdir'], 'cands_' + d['fileroot'] + '_sc' + str(d['scan']) + 'seg' + str(d['segment']) + '.pkl')
    elif segment >= 0:
        return os.path.join(d['workdir'], 'cands_' + d['fileroot'] + '_sc' + str(d['scan']) + 'seg' + str(segment) + '.pkl')
    else:
        return ''

def getnoisefile(d, segment=-1):
    """ Return name of noisefile for a given dictionary. Must have d['segment'] defined.
    """
    if d.has_key('segment'):
        return os.path.join(d['workdir'], 'noise_' + d['fileroot'] + '_sc' + str(d['scan']) + 'seg' + str(d['segment']) + '.pkl')
    elif segment >= 0:
        return os.path.join(d['workdir'], 'noise_' + d['fileroot'] + '_sc' + str(d['scan']) + 'seg' + str(segment) + '.pkl')
    else:
        return ''

def calc_nfalse(d):
    """ Calculate the number of thermal-noise false positives per segment.
    """

    dtfactor = n.sum([1./i for i in d['dtarr']])    # assumes dedisperse-all algorithm
    ntrials = d['readints'] * dtfactor * len(d['dmarr']) * d['npixx'] * d['npixy']
    qfrac = 1 - (erf(d['sigma_image1']/n.sqrt(2)) + 1)/2.
    nfalse = int(qfrac*ntrials)
    return nfalse

def calc_segment_times(d):
    """ Helper function for set_pipeline to define segmenttimes list, given nsegments definition
    """

    # this casts to int (flooring) to avoid 0.5 int rounding issue. 
    stopdts = n.linspace(d['nskip']+d['t_overlap']/d['inttime'], d['nints'], d['nsegments']+1)[1:]   # nseg+1 assures that at least one seg made
    startdts = n.concatenate( ([d['nskip']], stopdts[:-1]-d['t_overlap']/d['inttime']) )
            
    segmenttimes = []
    for (startdt, stopdt) in zip(d['inttime']*startdts, d['inttime']*stopdts):
        starttime = qa.getvalue(qa.convert(qa.time(qa.quantity(d['starttime_mjd']+startdt/(24*3600),'d'),form=['ymd'], prec=9)[0], 's'))[0]/(24*3600)
        stoptime = qa.getvalue(qa.convert(qa.time(qa.quantity(d['starttime_mjd']+stopdt/(24*3600), 'd'), form=['ymd'], prec=9)[0], 's'))[0]/(24*3600)
        segmenttimes.append((starttime, stoptime))
    d['segmenttimes'] = n.array(segmenttimes)
    totaltimeread = 24*3600*(d['segmenttimes'][:, 1] - d['segmenttimes'][:, 0]).sum()            # not guaranteed to be the same for each segment
    d['readints'] = n.round(totaltimeread / (d['inttime']*d['nsegments']*d['read_tdownsample'])).astype(int)
    d['t_segment'] = totaltimeread/d['nsegments']

def calc_memory_footprint(d, headroom=4., visonly=False):
    """ Given pipeline state dict, this function calculates the memory required
    to store visibilities and make images.
    headroom scales visibility memory size from single data object to all copies (and potential file read needs)
    Returns tuple of (vismem, immem) in units of GB.
    """

    toGB = 8/1024.**3   # number of complex64s to GB

    vismem = headroom * datasize(d) * toGB
    if visonly:
        return vismem
    else:
        immem = d['nthread'] * (d['readints']/d['nchunk'] * d['npixx'] * d['npixy']) * toGB
        return (vismem, immem)

def calc_fringetime(d):
    """ Estimate largest time span of a "segment".
    A segment is the maximal time span that can be have a single bg fringe subtracted and uv grid definition.
    Max fringe window estimated for 5% amp loss at first null averaged over all baselines. Assumes dec=+90, which is conservative.
    Returns time in seconds that defines good window.
    """

    maxbl = d['uvres']*d['npix']/2    # fringe time for imaged data only
    fringetime = 0.5*(24*3600)/(2*n.pi*maxbl/25.)   # max fringe window in seconds
    return fringetime

def correct_dmdt(d, dmind, dtind, blrange):
    """ Dedisperses and resamples data *in place*.
    Drops edges, since it assumes that data is read with overlapping chunks in time.
    """

    data = numpyview(data_mem, 'complex64', datashape(d))
    data_resamp = numpyview(data_resamp_mem, 'complex64', datashape(d))
    bl0,bl1 = blrange
    data_resamp[:, bl0:bl1] = data[:, bl0:bl1]
    rtlib.dedisperse_resample(data_resamp, d['freq'], d['inttime'], d['dmarr'][dmind], d['dtarr'][dtind], blrange, verbose=0)        # dedisperses data.

def calc_lm(d, im, pix=(), minmax='max'):
    """ Helper function to calculate location of image pixel in (l,m) coords.
    Assumes peak pixel, but input can be provided in pixel units.
    minmax defines whether to look for image maximum or minimum.
    """

    if len(pix) == 0:  # default is to get pixel from image
        if minmax == 'max':
            peakl, peakm = n.where(im == im.max())
        elif minmax == 'min':
            peakl, peakm = n.where(im == im.min())
        peakl = peakl[0]; peakm = peakm[0]
    elif len(pix) == 2:   # can also specify
        peakl, peakm = pix

    npixx, npixy = im.shape
    l1 = (npixx/2. - peakl)/(npixx*d['uvres'])
    m1 = (npixy/2. - peakm)/(npixy*d['uvres'])
    return l1, m1

def move_phasecenter(d, l1, m1, u, v):
    """ Handler function for phaseshift_threaded
    """

    data_resamp = numpyview(data_resamp_mem, 'complex64', datashape(d))
    rtlib.phaseshift_threaded(data_resamp, d, l1, m1, u, v)

def calc_dmgrid(d, maxloss=0.05, dt=3000., mindm=0., maxdm=0.):
    """ Function to calculate the DM values for a given maximum sensitivity loss.
    maxloss is sensitivity loss tolerated by dm bin width. dt is assumed pulse width in microsec.
    """

    # parameters
    tsamp = d['inttime']*1e6  # in microsec
    k = 8.3
    freq = d['freq'].mean()  # central (mean) frequency in GHz
    bw = 1e3*(d['freq'][-1] - d['freq'][0])
    ch = 1e3*(d['freq'][1] - d['freq'][0])  # channel width in MHz

    # width functions and loss factor
    dt0 = lambda dm: n.sqrt(dt**2 + tsamp**2 + ((k*dm*ch)/(freq**3))**2)
    dt1 = lambda dm, ddm: n.sqrt(dt**2 + tsamp**2 + ((k*dm*ch)/(freq**3))**2 + ((k*ddm*bw)/(freq**3.))**2)
    loss = lambda dm, ddm: 1-n.sqrt(dt0(dm)/dt1(dm,ddm))

    if maxdm == 0:
        return [0]
    else:
        # iterate over dmgrid to find optimal dm values. go higher than maxdm to be sure final list includes full range.
        dmgrid = n.arange(mindm, maxdm, 0.05)
        dmgrid_final = [dmgrid[0]]
        for i in range(len(dmgrid)):
            ddm = (dmgrid[i] - dmgrid_final[-1])/2.
            ll = loss(dmgrid[i],ddm)
            if ll > maxloss:
                dmgrid_final.append(dmgrid[i])

    return dmgrid_final

def image1(d, u, v, w, dmind, dtind, beamnum, irange):
    """ Parallelizable function for imaging a chunk of data for a single dm.
    Assumes data is dedispersed and resampled, so this just images each integration.
    Simple one-stage imaging that returns dict of params.
    returns dictionary with keys of cand location and values as tuple of features
    """

    i0, i1 = irange
    data_resamp = numpyview(data_resamp_mem, 'complex64', datashape(d))

    ims,snr,candints = rtlib.imgallfullfilterxyflux(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data_resamp[i0:i1], d['npixx'], d['npixy'], d['uvres'], d['sigma_image1'])

    feat = {}
    for i in xrange(len(candints)):
        if snr[i] > 0:
            l1, m1 = calc_lm(d, ims[i], minmax='max')
        else:
            l1, m1 = calc_lm(d, ims[i], minmax='min')
        logger.info('Got one!  Int=%d, DM=%d, dt=%d: SNR_im=%.1f @ (%.2e,%.2e).' % ((i0+candints[i])*d['dtarr'][dtind], d['dmarr'][dmind], d['dtarr'][dtind], snr[i], l1, m1))
        candid =  (d['segment'], (i0+candints[i])*d['dtarr'][dtind], dmind, dtind, beamnum)

        # assemble feature in requested order
        ff = []
        for feature in d['features']:
            if feature == 'snr1':
                ff.append(snr[i])
            elif feature == 'immax1':
                if snr[i] > 0:
                    ff.append(ims[i].max())
                else:
                    ff.append(ims[i].min())
            elif feature == 'l1':
                ff.append(l1)
            elif feature == 'm1':
                ff.append(m1)
            elif feature == 'im40':  # 40 pixel image peak cutout
                peakx, peaky = n.where(ims[i] == ims[i].max())
                sizex, sizey = ims[i].shape
                # set image window with min=0 and max=size
                xmin = max(0, peakx - 20); xmax = min(peakx + 20, sizex)   
                ymin = max(0, peaky - 20); ymax = min(peaky + 20, sizey)
                ff.append(ims[i][xmin:xmax,ymin:ymax])
            elif feature == 'spec20':  # 20 int spectrum cutout
                # set int window with min 0 and max len()
                imin = max(0, (i0+candints[i])*d['dtarr'][dtind] - 10)
                imax = min( (i0+candints[i])*d['dtarr'][dtind] + 10, len(data_resamp))
                data_cut = data_resamp[imin:imax].copy()
                rtlib.phaseshift_threaded(data_cut, d, l1, m1, u, v)
                ff.append(data_cut.mean(axis=1))
            elif feature in ['specstd', 'specskew', 'speckurtosis']:  # this is standard set and must all appear together
                if feature == 'specstd':  # first this one, then others will use same data
                    seli = (i0+candints[i])*d['dtarr'][dtind]
                    rtlib.phaseshift_threaded(data_resamp[seli:seli+1], d, l1, m1, u, v)
                    data = n.ma.masked_equal(data_resamp[seli:seli+1], 0j)
                    spec = data.mean(axis=3).mean(axis=1).mean(axis=0).real
                    std = spec.std(axis=0)
                    ff.append(std)
                elif feature == 'specskew':
                    skew = float(mstats.skew(spec))
                    ff.append(skew)
                elif feature == 'speckurtosis':
                    kurtosis = float(mstats.kurtosis(spec))
                    ff.append(kurtosis)
            elif feature == 'imskew':
                skew = float(mstats.skew(ims[i].flatten()))
                ff.append(skew)
            elif feature == 'imkurtosis':
                kurtosis = float(mstats.kurtosis(ims[i].flatten()))
                ff.append(kurtosis)

        feat[candid] = list(ff)
    return feat

def image2(d, i0, i1, u, v, w, dmind, dtind, beamnum):
    """ Parallelizable function for imaging a chunk of data for a single dm.
    Assumes data is dedispersed and resampled, so this just images each integration.
    Two-stage imaging uses ideal uv coverage in second image.
    returns dictionary with keys of cand location and values as tuple of features
    """

    data_resamp = numpyview(data_resamp_mem, 'complex64', datashape(d))
    ims,snr,candints = rtlib.imgallfullfilterxy(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data_resamp[i0:i1], d['npixx'], d['npixy'], d['uvres'], d['sigma_image1'])

    feat = {}
    for i in xrange(len(candints)):
        # reimage
        im2 = rtlib.imgonefullxy(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data_resamp[i0+candints[i]], d['npixx_full'], d['npixy_full'], d['uvres'], verbose=0)

        # find most extreme pixel
        snrmax = im2.max()/im2.std()
        snrmin = im2.min()/im2.std()
        if snrmax >= abs(snrmin):
            snr2 = snrmax
        else:
            snr2 = snrmin
        # threshold
        if abs(snr2) > d['sigma_image2']:
            # calc loc in first image
            if snr[i] > 0:
                l1, m1 = calc_lm(d, ims[i], minmax='max')
            else:
                l1, m1 = calc_lm(d, ims[i], minmax='min')

            # calc src loc in second image
            if snr2 > 0:
                l2, m2 = calc_lm(d, im2, minmax='max')
            else:
                l2, m2 = calc_lm(d, im2, minmax='min')
            logger.info('Got one!  Int=%d, DM=%d, dt=%d: SNR_im1=%.1f, SNR_im2=%.1f @ (%.2e,%.2e).' % ((i0+candints[i])*d['dtarr'][dtind], d['dmarr'][dmind], d['dtarr'][dtind], snr[i], snr2, l2, m2))
            candid =  (d['segment'], (i0+candints[i])*d['dtarr'][dtind], dmind, dtind, beamnum)

            # assemble feature in requested order
            ff = []
            for feature in d['features']:
                if feature == 'snr1':
                    ff.append(snr[i])
                elif feature == 'immax1':
                    if snr[i] > 0:
                        ff.append(ims[i].max())
                    else:
                        ff.append(ims[i].min())
                elif feature == 'l1':
                    ff.append(l1)
                elif feature == 'm1':
                    ff.append(m1)
                elif feature == 'snr2':
                    ff.append(snr2)
                elif feature == 'immax2':
                    if snr2 > 0:
                        ff.append(im2.max())
                    else:
                        ff.append(im2.min())
                elif feature == 'l2':
                    ff.append(l2)
                elif feature == 'm2':
                    ff.append(m2)

            feat[candid] = list(ff)
        else:
            logger.info('Almost...  Int=%d, DM=%d, dt=%d: SNR_im1=%.1f, SNR_im2=%.1f.' % ((i0+candints[i])*d['dtarr'][dtind], d['dmarr'][dmind], d['dtarr'][dtind], snr[i], snr2))

    return feat

def image2w(d, i0, i1, u, v, w, dmind, dtind, beamnum, bls, uvkers):
    """ Parallelizable function for imaging a chunk of data for a single dm.
    Assumes data is dedispersed and resampled, so this just images each integration.
    Two-stage imaging uses ideal uv coverage in second image.
    returns dictionary with keys of cand location and values as tuple of features
    """

    data_resamp = numpyview(data_resamp_mem, 'complex64', datashape(d))
    ims,snr,candints = rtlib.imgallfullfilterxy(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data_resamp[i0:i1], d['npixx'], d['npixy'], d['uvres'], d['sigma_image1'])

    feat = {}
    for i in xrange(len(candints)):
        # reimage
        npix = max(d['npixx_full'], d['npixy_full'])
        im2 = rtlib.imgonefullw(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data_resamp[i0+candints[i]], npix, d['uvres'], bls, uvkers, verbose=1)
        logger.debug(im2.shape)

        # find most extreme pixel
        snrmax = im2.max()/im2.std()
        snrmin = im2.min()/im2.std()
        if snrmax >= abs(snrmin):
            snr2 = snrmax
        else:
            snr2 = snrmin
        # threshold
        if abs(snr2) > d['sigma_image2']:
            # calc loc in first image
            if snr[i] > 0:
                l1, m1 = calc_lm(d, ims[i], minmax='max')
            else:
                l1, m1 = calc_lm(d, ims[i], minmax='min')

            # calc src loc in second image
            if snr2 > 0:
                l2, m2 = calc_lm(d, im2, minmax='max')
            else:
                l2, m2 = calc_lm(d, im2, minmax='min')
            logger.info('Got one!  Int=%d, DM=%d, dt=%d: SNR_im1=%.1f, SNR_im2=%.1f @ (%.2e,%.2e).' % ((i0+candints[i])*d['dtarr'][dtind], d['dmarr'][dmind], d['dtarr'][dtind], snr[i], snr2, l2, m2))
            candid =  (d['segment'], (i0+candints[i])*d['dtarr'][dtind], dmind, dtind, beamnum)

            # assemble feature in requested order
            ff = []
            for feature in d['features']:
                if feature == 'snr1':
                    ff.append(snr[i])
                elif feature == 'immax1':
                    if snr[i] > 0:
                        ff.append(ims[i].max())
                    else:
                        ff.append(ims[i].min())
                elif feature == 'l1':
                    ff.append(l1)
                elif feature == 'm1':
                    ff.append(m1)
                elif feature == 'snr2':
                    ff.append(snr2)
                elif feature == 'immax2':
                    if snr2 > 0:
                        ff.append(im2.max())
                    else:
                        ff.append(im2.min())
                elif feature == 'l2':
                    ff.append(l2)
                elif feature == 'm2':
                    ff.append(m2)

            feat[candid] = list(ff)
        else:
            logger.info('Almost...  Int=%d, DM=%d, dt=%d: SNR_im1=%.1f, SNR_im2=%.1f.' % ((i0+candints[i])*d['dtarr'][dtind], d['dmarr'][dmind], d['dtarr'][dtind], snr[i], snr2))

    return feat

def image1wrap(d, u, v, w, npixx, npixy, candint):
    """ Parallelizable function for imaging a chunk of data for a single dm.
    Assumes data is dedispersed and resampled, so this just images each integration.
    Simple one-stage imaging that returns dict of params.
    returns dictionary with keys of cand location and values as tuple of features
    """

    data_resamp = numpyview(data_resamp_mem, 'complex64', datashape(d))
    image = rtlib.imgonefullxy(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data_resamp[candint], npixx, npixy, d['uvres'], verbose=1)
    return image

def sample_image(d, data, u, v, w, i=-1, verbose=1, imager='xy', wres=100):
    """ Samples one integration and returns image
    i is integration to image. Default is mid int.
    """

    if i == -1:
        i = len(data)/2

    if imager == 'xy':
        image = rtlib.imgonefullxy(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data[i], d['npixx'], d['npixy'], d['uvres'], verbose=verbose)
    elif imager == 'w':
        npix = max(d['npixx'], d['npixy'])
        bls, uvkers = rtlib.genuvkernels(w, wres, npix, d['uvres'], ksize=21, oversample=1)
        image = rtlib.imgonefullw(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data[i], npix, d['uvres'], bls, uvkers, verbose=verbose)

#        bls, lmkers = rtlib.genlmkernels(w, wres, npix, d['uvres'])
#        image = rtlib.imgonefullw(n.outer(u, d['freq']/d['freq_orig'][0]), n.outer(v, d['freq']/d['freq_orig'][0]), data[i], npix, d['uvres'], [bls[0]], [lmkers[0]], verbose=verbose)

    return image

def estimate_noiseperbl(data):
    """ Takes large data array and sigma clips it to find noise per bl for input to detect_bispectra.
    Takes mean across pols and channels for now, as in detect_bispectra.
    """
    
    # define noise per baseline for data seen by detect_bispectra or image
    datamean = data.mean(axis=2).imag                      # use imaginary part to estimate noise without calibrated, on-axis signal
    (datameanmin, datameanmax) = rtlib.sigma_clip(datamean.flatten())
    good = n.where( (datamean>datameanmin) & (datamean<datameanmax) )
    noiseperbl = datamean[good].std()   # measure single noise for input to detect_bispectra
    logger.debug('Clipped to %d%% of data (%.3f to %.3f). Noise = %.3f.' % (100.*len(good[0])/len(datamean.flatten()), datameanmin, datameanmax, noiseperbl))
    return noiseperbl

def noisepickle(d, data, u, v, w, chunk=200):
    """ Calculates noise properties and saves values to pickle.
    chunk defines window for measurement. at least one measurement always made.
    """

    if d['savenoise']:
        noisefile = getnoisefile(d)

        if os.path.exists(noisefile):
            logger.warn('noisefile %s already exists' % noisefile)
        else:
            nints = len(data)
            chunk = min(chunk, nints)  # ensure at least one measurement
            results = []

            rr = range(0, nints, chunk)
            if len(rr) == 1: rr.append(1)   # hack. need to make sure it iterates for nints=1 case
            for i in range(len(rr)-1):
                imid = (rr[i]+rr[i+1])/2
                noiseperbl = estimate_noiseperbl(data[rr[i]:rr[i+1]])
                imstd = sample_image(d, data, u, v, w, imid, verbose=0).std()
                zerofrac = float(len(n.where(data[rr[i]:rr[i+1]] == 0j)[0]))/data[rr[i]:rr[i+1]].size
                results.append( (d['segment'], noiseperbl, zerofrac, imstd) )

            with open(noisefile, 'a') as pkl:
                pickle.dump(results, pkl)
            logger.info('Wrote %d noise measurement%s to %s.' % (len(results), 's'[:len(results)-1], noisefile))

def savecands(d, cands):
    """ Save all candidates in pkl file for later aggregation and filtering.
    """

    with open(getcandsfile(d), 'w') as pkl:
        pickle.dump(d, pkl)
        pickle.dump(cands, pkl)

def datashape(d):
    return (d['readints'], d['nbl'], d['nchan'], d['npol'])

def datasize(d):
    return long(d['readints']*d['nbl']*d['nchan']*d['npol'])

def numpyview(arr, datatype, shape, raw=False):
    """ Takes mp shared array and returns numpy array with given shape.
    """

    if raw:
        return n.frombuffer(arr, dtype=n.dtype(datatype)).view(n.dtype(datatype)).reshape(shape)   # for shared mps.RawArray
    else:
        return n.frombuffer(arr.get_obj(), dtype=n.dtype(datatype)).view(n.dtype(datatype)).reshape(shape)  # for shared mp.Array

def initreadonly(shared_arr_):
    global data_read_mem
    data_read_mem = shared_arr_ # must be inhereted, not passed as an argument

def initresamp(shared_arr_, shared_arr2_):
    global data_mem, data_resamp_mem
    data_mem = shared_arr_
    data_resamp_mem = shared_arr2_

def initread(shared_arr1_, shared_arr2_, shared_arr3_, shared_arr4_, shared_arr5_, shared_arr6_, shared_arr7_, shared_arr8_):
    global data_read_mem, u_read_mem, v_read_mem, w_read_mem, data_mem, u_mem, v_mem, w_mem
    data_read_mem = shared_arr1_  # must be inhereted, not passed as an argument
    u_read_mem = shared_arr2_
    v_read_mem = shared_arr3_
    w_read_mem = shared_arr4_
    data_mem = shared_arr5_
    u_mem = shared_arr6_
    v_mem = shared_arr7_
    w_mem = shared_arr8_
