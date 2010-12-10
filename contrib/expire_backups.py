#!/usr/bin/env python
'''
expire_backups.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import


import sys
import os
import logging
import re
import textwrap
import shutil
import cPickle as pickle
from datetime import datetime, timedelta
from collections import defaultdict

# We are running from the S3QL source directory, make sure
# that we use modules from this directory
basedir = os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]), '..'))
if (os.path.exists(os.path.join(basedir, 'setup.py')) and
    os.path.exists(os.path.join(basedir, 'src', 's3ql', '__init__.py'))):
    sys.path = [os.path.join(basedir, 'src')] + sys.path
    
from s3ql.common import setup_logging
from s3ql.parse_args import ArgumentParser
from s3ql.cli.remove import main as s3qlrm
    
log = logging.getLogger('expire_backups')


def parse_args(args):
    '''Parse command line'''

    parser = ArgumentParser(
        description=textwrap.dedent('''\
        ``expire_backups.py`` is a program to intelligently remove old backups
        that are no longer needed.

        To define what backups you want to keep for how long, you define a
        number of *age ranges*. ``expire_backups`` ensures that you will
        have at least one backup in each age range at all times. It will keep
        exactly as many backups as are required for that and delete any
        backups that become redundant.
        
        Age ranges are specified by giving a list of range boundaries in terms
        of backup cycles. Every time you create a new backup, the existing
        backups age by one cycle.

        Please refer to the S3QL documentation for details.
        '''))

    parser.add_quiet()
    parser.add_debug()
    parser.add_version() 

    parser.add_argument('cycles', nargs='+',  type=int, metavar='<age>',
                        help='Age range boundaries in terms of backup cycles')
    parser.add_argument('--state', metavar='<file>', type=str,
                        default='./.expire_backups.dat',
                        help='File to save state information in (default: %(default)s')
    parser.add_argument("-n", action="store_true", default=False,
                        help="Dry run. Just show which backups would be deleted.")

    parser.add_argument("--use-s3qlrm", action="store_true",
                      help="Use `s3qlrm` command to delete backups.")
    
    options = parser.parse_args(args) 
    
    if sorted(options.cycles) != options.cycles:
        parser.error('Age range boundaries must be in increasing order')
        
    return options

def main(args=None):

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    setup_logging(options)
    
    # Determine available backups
    backup_list = set(x for x in os.listdir('.')
                      if re.match(r'^\d{4}-\d\d-\d\d_\d\d:\d\d:\d\d$', x))

    if not os.path.exists(options.state):
        log.warn('No existing state file, assuming first-time run.')
        if len(backup_list) > 1:
            state = upgrade_to_state(backup_list)
        else:
            state = dict()
    else:
        log.info('Reading state...')
        state = pickle.load(open(options.state, 'rb'))
            
    to_delete = process_backups(backup_list, state, options.cycles)
   
    for x in to_delete:
        log.info('Backup %s is no longer needed, removing...', x)
        if not options.n:
            if options.use_s3qlrm:
                s3qlrm([x])
            else:
                shutil.rmtree(x)
           
    if options.n:
        log.info('Dry run, not saving state.')
    else: 
        log.info('Saving state..')    
        pickle.dump(state, open(options.state, 'wb'), 2)
       
def upgrade_to_state(backup_list):
    log.info('Several existing backups detected, trying to convert absolute ages to cycles')
    
    now = datetime.now()
    age = dict()
    for x in sorted(backup_list):
        age[x] = now - datetime.strptime(x, '%Y-%m-%d_%H:%M:%S')
        log.info('Backup %s is %s hours old', x, age[x])
        
    deltas = [ abs(x - y) for x in age.itervalues() 
                          for y in age.itervalues() if x != y ]
    step = min(deltas)   
    log.info('Assuming backup interval of %s hours', step)
    
    state = dict()
    for x in sorted(age):
        state[x] = 0
        while age[x] > timedelta(0):
            state[x] += 1
            age[x] -= step
        log.info('Backup %s is %d cycles old', x, state[x])
        
    log.info('State construction complete.')
    return state
                        
def simulate(args):

    options = parse_args(args)
    setup_logging(options)
        
    state = dict()
    backup_list = set()
    for i in xrange(50):
        backup_list.add('backup-%2d' % i)
        delete = process_backups(backup_list, state, options.cycles)
        log.info('Deleting %s', delete)
        backup_list -= delete

        log.info('Available backups on day %d:', i)
        for x in sorted(backup_list):
            log.info(x)  
    
def process_backups(backup_list, state, cycles):
        
    # New backups
    new_backups = backup_list - set(state)
    for x in sorted(new_backups):
        log.info('Found new backup %s', x)
        for y in state:
            state[y] += 1
        state[x] = 0
    
    for x in state:
        log.debug('Backup %s has age %d', x, state[x])
                
    # Missing backups
    missing_backups = set(state) - backup_list
    for x in missing_backups:
        log.warn('Warning: backup %s is missing. Did you delete it manually?', x)
        del state[x]

    # Ranges
    ranges = [ (0, cycles[0]) ]
    for i in range(1, len(cycles)):
        ranges.append((cycles[i-1], cycles[i]))
    
    # Go forward in time to see what backups need to be kept
    simstate = dict()
    keep = set()
    missing = defaultdict(list)
    for step in xrange(max(cycles)):
        
        log.debug('Considering situation after %d more backups', step)
        for x in simstate:
            simstate[x] += 1
            log.debug('Backup x now has simulated age %d', simstate[x])
            
        # Add the hypothetical backup that has been made "just now"
        if step != 0:
            simstate[step] = 0
        
        for (min_, max_) in ranges:
            log.debug('Looking for backup for age range %d to %d', min_, max_)
            
            # Look in simstate
            found = False
            for (backup, age) in simstate.iteritems():
                if min_ <= age < max_:
                    found = True
                    break
            if found:
                # backup and age will be defined
                #pylint: disable=W0631
                log.debug('Using backup %s (age %d)', backup, age)
                continue
            
            # Look in state
            for (backup, age) in state.iteritems():
                age += step
                if min_ <= age < max_:
                    log.info('Keeping backup %s (current age %d) for age range %d to %d%s',
                             backup, state[backup], min_, max_,
                             (' in %d cycles' % step) if step else '')
                    simstate[backup] = age
                    keep.add(backup)
                    break
                
            else:
                if step == 0:
                    log.info('Note: there is currently no backup available '
                             'for age range %d to %d', min_, max_)
                else:
                    missing['%d to %d' % (min_, max_)].append(step)
                    
    for range_ in sorted(missing):
        log.info('Note: there will be no backup for age range %s '
                 'in (forthcoming) cycle(s): %s', 
                 range_, format_list(missing[range_]))
  
    to_delete = set(state) - keep
    for x in to_delete:
        del state[x]
        
    return to_delete

    
def format_list(l):
    if not l:
        return ''
    l = l[:]
    
    # Append bogus end element
    l.append(l[-1] + 2)
    
    range_start = l.pop(0)
    cur = range_start
    res = list()
    for n in l:
        if n == cur+1:
            pass
        elif range_start == cur:
            res.append('%d' % cur)
        elif range_start == cur - 1:
            res.append('%d' % range_start)
            res.append('%d' % cur)
        else:
            res.append('%d-%d' % (range_start, cur))
        
        if n != cur+1:
            range_start = n
        cur = n
            
    if len(res) > 1:
        return ('%s and %s' % (', '.join(res[:-1]), res[-1])) 
    else:
        return ', '.join(res)
        
        
if __name__ == '__main__':
    #simulate(sys.argv[1:])
    main(sys.argv[1:])