#!/usr/bin/env python
"""
Functions for using PyPolyChord, including using it to perform dynamic nested
sampling.
"""
import os
import time
import shutil
import copy
import numpy as np
import scipy.signal
import nestcheck.data_processing
import nestcheck.io_utils as iou
import dyPolyChord.nlive_allocation


def run_dypolychord(run_func, settings_dict_in, dynamic_goal, **kwargs):
    """
    Dynamic nested sampling targeting increased parameter estimation accuracy
    using polychord.

    PolyChord checks nlives to see if point should be added or
    removed every single step.

    The dynamic run's live points are determined by the dynamic ns importance
    calculation.
    Both the initial and dynamic runs use settings.nlive = ninit to determine
    clustering and resume writing.
    """
    default_settings = {'nlive': 100,
                        'num_repeats': 20,
                        'file_root': 'temp',
                        'base_dir': 'chains',
                        'seed': -1,
                        'do_clustering': True,
                        'max_ndead': -1,
                        'write_dead': True,
                        'write_stats': True,
                        'write_live': False,
                        'write_paramnames': False,
                        'equals': False,
                        'cluster_posteriors': False,
                        'nlives': {},
                        'write_resume': False,
                        'read_resume': False}
    for key, value in default_settings.items():
        if key not in settings_dict_in:
            settings_dict_in[key] = value
    ninit = kwargs.pop('ninit', 10)
    init_step = kwargs.pop('init_step', ninit)
    nlive_const = kwargs.pop('nlive_const', settings_dict_in['nlive'])
    print_time = kwargs.pop('print_time', False)
    seed_increment = kwargs.pop('seed_increment', 100)
    default_smoothing = (lambda x: scipy.signal.savgol_filter(
        x, 1 + (2 * ninit), 3, mode='nearest'))
    smoothing_filter = kwargs.pop('smoothing_filter', default_smoothing)
    if kwargs:
        raise TypeError('Unexpected **kwargs: {0}'.format(kwargs))
    start_time = time.time()
    assert not settings_dict_in['nlives']
    assert not settings_dict_in['read_resume']
    root = os.path.join(settings_dict_in['base_dir'],
                        settings_dict_in['file_root'])
    # Step 1: do initial run
    # ----------------------
    settings_dict = copy.deepcopy(settings_dict_in)  # so we dont edit settings
    settings_dict['file_root'] = settings_dict_in['file_root'] + '_init'
    settings_dict['nlive'] = ninit
    if dynamic_goal == 0:
        run_func(settings_dict)
    else:
        settings_dict['write_resume'] = True
        settings_dict['read_resume'] = False
        add_points = True
        step_ndead = []
        outputs_at_resumes = {}
        while add_points:
            if len(step_ndead) == 1:
                settings_dict['read_resume'] = True
            settings_dict['max_ndead'] = (len(step_ndead) + 1) * init_step
            if settings_dict['seed'] >= 0:
                settings_dict['seed'] += seed_increment
            run_func(settings_dict)
            run_output = nestcheck.data_processing.process_polychord_stats(
                settings_dict['file_root'], settings_dict['base_dir'])
            # store run outputs for use getting nlike
            outputs_at_resumes[run_output['ndead']] = run_output
            step_ndead.append(run_output['ndead'] - settings_dict['nlive'])
            if len(step_ndead) >= 2:
                if step_ndead[-1] == step_ndead[-2]:
                    break
            # store resume file in new file path
            shutil.copyfile(root + '_init.resume',
                            root + '_init_' + str(step_ndead[-1]) + '.resume')
        print(outputs_at_resumes.keys())
    # Step 2: calculate an allocation of live points
    # ----------------------------------------------
    init_run = nestcheck.data_processing.process_polychord_run(
        settings_dict_in['file_root'] + '_init', settings_dict_in['base_dir'])
    # Calculate max number of samples
    if settings_dict_in['max_ndead'] > 0:
        samp_tot = settings_dict_in['max_ndead']
        assert settings_dict_in['max_ndead'] > init_run['logl'].shape[0], (
            'all points used in initial run and none left for dynamic run!')
    else:
        samp_tot = init_run['logl'].shape[0] * (nlive_const / ninit)
        assert nlive_const > ninit
    dyn_info = dyPolyChord.nlive_allocation.allocate(
        init_run, samp_tot, dynamic_goal, smoothing_filter=smoothing_filter)
    if dyn_info['peak_start_ind'] != 0:
        # subtract 1 as ndead=1 corresponds to point 0
        resume_steps = np.asarray(step_ndead) - 1
        print('resume_steps:', resume_steps, 'peak_start_ind:',
              dyn_info['peak_start_ind'])
        # Load the last resume before we reach the peak
        resume_ndead = step_ndead[np.where(
            resume_steps < dyn_info['peak_start_ind'])[0][-1]]
        # copy resume step to dynamic file root
        shutil.copyfile(root + '_init_' + str(resume_ndead) + '.resume',
                        root + '_dyn.resume')
    if dynamic_goal != 0:
        # Remove all the temporary resume files. Use set to avoid duplicates as
        # these cause OSErrors.
        for snd in set(step_ndead):
            os.remove(root + '_init_' + str(snd) + '.resume')
    # Step 3: do dynamic run
    # ----------------------
    final_init_seed = settings_dict['seed']
    settings_dict = copy.deepcopy(settings_dict_in)  # remove edits from init
    if final_init_seed >= 0:
        assert settings_dict_in['seed'] >= 0, (
            'if input seed was <0 it should not have been edited')
        settings_dict['seed'] = final_init_seed + seed_increment
    if dynamic_goal == 0:
        settings_dict['nlive'] = max(dyn_info['nlives_dict'].values())
    else:
        settings_dict['nlive'] = ninit
    settings_dict['nlives'] = dyn_info['nlives_dict']
    settings_dict['read_resume'] = (dyn_info['peak_start_ind'] != 0)
    settings_dict['file_root'] = settings_dict_in['file_root'] + '_dyn'
    run_func(settings_dict)
    if dyn_info['peak_start_ind'] != 0:
        # Save info about where the dynamic run was resumed from
        dyn_info['resume_ndead'] = resume_ndead
        dyn_info['resume_nlike'] = outputs_at_resumes[resume_ndead]['nlike']
    iou.pickle_save(dyn_info, root + '_dyn_info', overwrite_existing=True)
    # tidy up remaining .resume files (if the function has reach this point,
    # both the initial and dynamic runs have finished so we shouldn't need to
    # resume)
    for extra in ['init', 'dyn']:
        try:
            os.remove(root + '_{0}.resume'.format(extra))
        except FileNotFoundError:
            pass
    if print_time:
        end_time = time.time()
        print('##########################################')
        print('run_dypolychord_param took {} sec'
              .format(end_time - start_time))
        print('file_root = ' + settings_dict_in['file_root'])
        print('##########################################')
