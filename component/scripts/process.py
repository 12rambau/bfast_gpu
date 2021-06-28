import threading
from concurrent import futures
from datetime import datetime as dt
from functools import partial
import time
import json
from pathlib import Path

import rasterio as rio
import numpy as np
from bfast import BFASTMonitor
from bfast.monitor.utils import crop_data_dates
from osgeo import gdal

from component import parameter as cp
from component.message import cm

def debug(data, dates, segment_dir, loc_monitor_params, write_lock):
    
    with write_lock:
        # set the debugging folder 
        debug_dir = cp.result_dir/'bfast_gpu_debug'
        debug_dir.mkdir(exist_ok=True)

        # extract the time serie name and create a folder 
        ts_dir = debug_dir/segment_dir.parts[-2]
        ts_dir.mkdir(exist_ok=True)

        # extract the tile number 
        tile_dir = ts_dir/segment_dir.parts[-1]
        tile_dir.mkdir(exist_ok=True)
    
    # get the number of folder in the tile and create a new one 
    number = len([f for f in tile_dir.iterdir() if f.is_dir()])
    save_folder = tile_dir/f'{number}'
    save_folder.mkdir()
    
    # save monitoring parameters
    moitor_params = {k: v if type(v) != dt else v.strftime("%Y-%m-%d") for k, v in loc_monitor_params.items()}
    (save_folder/'monitor_params.json').write_text(json.dumps(moitor_params, indent=4))
    
    # save dates 
    (save_folder/'dates.json').write_text(json.dumps([d.strftime("%Y-%m-%d") for d in dates], indent=4))
    
    # save data 
    np.save((save_folder/'data.npy'), data)
    
    return

def break_to_decimal_year(idx, dates):
    """
    break dates of change into decimal year
    build to be vectorized over the resulting bfast break events
    using the following format 
    """
    
    # everything could have been done in a lambda function but for the sake of clarity I prefer to use it 
    if idx < 0:
        return np.nan
    else:
        break_date = dates[idx-1]
        return break_date.year + (break_date.timetuple().tm_yday - 1)/365
    
def bfast_stack(stack, segment_dir, save_dir, monitor_params, crop_params, out):
    """Run the bfast model on image windows"""
    
    # get stack name without the tile_ prefix
    stack_id = stack.parent.stem.replace('tile-', '')
    
    # read the stack file
    with rio.open(stack) as src:
        
        # read the local observation date
        with (segment_dir/'dates.csv').open() as f:
            dates = sorted([dt.strptime(l, "%Y-%m-%d") for l in f.read().splitlines() if l.rstrip()])
        
        # update the crop and bfast params with the current tile dates 
        crop_params = {k: next(d for d in dates if d > val) for k, val in crop_params.items()}
        loc_monitor_params = {**monitor_params, 'start_monitor': next(d for d in dates if d > monitor_params['start_monitor'])}
        
        # split computation into small windows 
        for window in [w for _, w in src.block_windows()]:
    
            data = src.read(window=window).astype(np.int16)
            # all the nan are transformed into 0 by casting don't we want to use np.iinfo.minint16 ?
            
            # crop the initial data to the used dates
            data, dates = crop_data_dates(data,  dates, **crop_params)
    
            # start the bfast process
            model = BFASTMonitor(**loc_monitor_params)
        
            # fit the model 
            model.fit(data, dates)

            # vectorized fonction to format the results as decimal year (e.g mid 2015 will be 2015.5)
            to_decimal = np.vectorize(break_to_decimal_year, excluded=[1])
    
            # slice the date to narrow it to the monitoring dates
            start = loc_monitor_params['start_monitor']
            end = crop_params['end']
            monitoring_dates = dates[dates.index(start):dates.index(end)+1] # carreful slicing is x in [i,j[ 
    
            # compute the decimal break on the model 
            decimal_breaks = to_decimal(model.breaks, monitoring_dates)
    
            # agregate the results on 2 bands
            monitoring_results = np.stack((decimal_breaks, model.magnitudes)).astype(np.float32)
            
            # get the profile from the source
            profile = src.profile.copy()
            profile.update(
                driver = 'GTiff',
                count = 2,
                dtype = np.float32
            )
    
            with rio.open(save_dir/f'bfast_outputs_{stack_id}.tif', 'w', **profile) as dst:
                dst.write(monitoring_results, window=window)
        
            out.update_progress()
        
    return

def bfast_window(window, read_lock, write_lock, src, dst, segment_dir, monitor_params, crop_params, out):
    """Run the bfast model on image windows"""
    
    # read in a read_lock to avoid duplicate reading and corruption of the data
    with read_lock:
        data = src.read(window=window).astype(np.int16)
        # all the nan are transformed into 0 by casting don't we want to use np.iinfo.minint16 ? 
    
    # read the local observation date
    with (segment_dir/'dates.csv').open() as f:
        dates = sorted([dt.strptime(l, "%Y-%m-%d") for l in f.read().splitlines() if l.rstrip()])
        
    # update the crop and bfast params with the current tile dates 
    crop_params = {k: next(d for d in dates if d > val) for k, val in crop_params.items()}
    loc_monitor_params = {**monitor_params, 'start_monitor': next(d for d in dates if d > monitor_params['start_monitor'])}
        
    # crop the initial data to the used dates
    data, dates = crop_data_dates(data,  dates, **crop_params)
    
    #with write_lock: 
    #    out.add_live_msg("creating the model")
    
    # start the bfast process
    model = BFASTMonitor(**loc_monitor_params)
    
    #with write_lock: 
    #    out.add_live_msg("fitting the model")
        
    # fit the model 
    model.fit(data, dates)
    
    #with write_lock: 
    #    out.add_live_msg("check for NaN")
    
    # test if magnitude exist
    # if yes log the incriminated window parameter to further investigation
    #if np.isnan(model.magnitudes).all():
    #    
    #    with write_lock: 
    #        out.add_live_msg("write debug info")
    #       
    #    debug(data, dates, segment_dir, loc_monitor_params, write_lock)

    # vectorized fonction to format the results as decimal year (e.g mid 2015 will be 2015.5)
    to_decimal = np.vectorize(break_to_decimal_year, excluded=[1])
    
    # slice the date to narrow it to the monitoring dates
    start = loc_monitor_params['start_monitor']
    end = crop_params['end']
    monitoring_dates = dates[dates.index(start):dates.index(end)+1] # carreful slicing is x in [i,j[ 
    
    # compute the decimal break on the model 
    decimal_breaks = to_decimal(model.breaks, monitoring_dates)
    
    # agregate the results on 2 bands
    monitoring_results = np.stack((decimal_breaks, model.magnitudes)).astype(np.float32)
    
    with write_lock:
        dst.write(monitoring_results, window=window)
        out.update_progress()
        
    return
        
def run_bfast(folder, out_dir, tiles, monitoring, history, freq, k, hfrac, trend, level, backend, out):
    """pilot the different threads that will launch the bfast process on windows"""
    
    # prepare parameters for crop as a dict 
    crop_params = {
        'start': history,
        'end': monitoring[1]
    }
        
    # prepare parameters for the bfastmonitor function 
    monitor_params = {
        'start_monitor': monitoring[0],
        'freq': freq,
        'k': k,
        'hfrac': hfrac,
        'trend': trend,
        'level': 1-level,  # it's an hidden parameter I hate it https://github.com/diku-dk/bfast/issues/23
        'backend': backend
    }
    
    # create 1 folder for each set of parameter
    parameter_string = f'{history.year}_{monitoring[0].year}_{monitoring[1].year}_k{k}_f{freq}_t{int(trend)}_h{hfrac}_l{level}'
    save_dir = cp.result_dir/out_dir/parameter_string
    save_dir.mkdir(parents=True, exist_ok=True)
    
    # loop through the tiles
    file_list = []
    for tile in tiles:
        
        # get the starting time 
        start = dt.now()
        
        # get the segment useful folders 
        tile_dir = folder/tile
        tile_save_dir = save_dir/tile
        tile_save_dir.mkdir(exist_ok=True)
        
        # set the log and output file names
        log_file = tile_save_dir/f'tile_{tile}.log'
        file = tile_save_dir/'bfast_outputs.tif'
        
        # check the logs to see if the tile is already finished 
        if log_file.is_file():
            out.add_msg(cm.bfast.skip.format(tile))
            time.sleep(.5) # to let people read the message
            file_list.append(str(file))
            continue
        
        # create the locks to avoid data coruption
        read_lock = threading.Lock()
        write_lock = threading.Lock()
        
        # count the number of windows for advancement display
        sub_stack_files = list(folder.glob('*/tile-*/stack.vrt'))
        
        count = 0
        for i, stack in enumerate(sub_stack_files):
            
            with rio.open(stack, GEOREF_SOURCES='INTERNAL') as src:
                count += sum(1 for _ in src.block_windows())
                
        out.add_live_msg(cm.bfast.sum_up.format(count, tile))
        out.reset_progress(count, cm.bfast.progress.format(tile))
        
        # launch bfast on tiles in parralel
        bfast_params = {
            'save_dir': tile_save_dir,
            'segment_dir': tile_dir, 
            'monitor_params': monitor_params, 
            'crop_params': crop_params,
            'out': out
        }
        
        # test outside the future
        #for stack in sub_stack_files:
        #    bfast_stack(stack, **bfast_params)
        #    raise Exception ("done")
            
        with futures.ThreadPoolExecutor() as executor: # use all the available CPU/GPU
            executor.map(partial(bfast_stack, **bfast_params), sub_stack_files)
            
        # write in the logs that the tile is finished
        write_logs(log_file, start, dt.now())
        
        # get the file list
        file_list = [str(f) for f in save_dir.glob('*/bfast_outputs_*.tif')]
        
        # write a global vrt file to open all the tile at once
        vrt_path = save_dir/f'bfast_outputs_{out_dir}.vrt'
        ds = gdal.BuildVRT(str(vrt_path), file_list)
        ds.FlushCache()
        
        # check that the file was effectively created (gdal doesn't raise errors)
        if not vrt_path.is_file():
            raise Exception(f"the vrt {vrt_path} was not created")
           
    return 

def write_logs(log_file, start, end):
    
    with log_file.open('w') as f: 
        f.write("Computation finished!\n")
        f.write("\n")
        f.write(f"Computation started on: {start} \n")
        f.write(f"Computation finished on: {end}\n")
        f.write("\n")
        f.write(f"Elapsed time: {end-start}")
        
    return
    
    
        
        
        
