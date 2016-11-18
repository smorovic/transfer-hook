#!/bin/env python

# Last modified by Dylan G. Hsu on 2015-05-29 :: dylan.hsu@cern.ch

import os,sys,socket
import shutil
import time,datetime
import cx_Oracle
import json
import logging
import signal
#import multiprocessing

import smhook.config

# Hardcoded Config file to be used, is defined below:
# We read from production DB no matter what (in either case)
# but for testing, write to integration DB only
debug=True
myconfig = os.path.join(smhook.config.DIR, '.db_rates_integration.py')
#myconfig = os.path.join(smhook.config.DIR, '.db_rates_production.py')
cxn_timeout = 60*60 # Timeout for database connection in seconds
num_retries = 5
query_timeout = 2 #Timeout for individual queries in seconds

logger = logging.getLogger(__name__)
# For debugging purposes, initialize the logger to stdout if running script as a standalone
if debug == True:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    logger.addHandler(ch)

# Load the config
logger.info('Using config: %s' % myconfig)
execfile(myconfig)

# Establish DB connections as module globals
# This allows persistent database connections
global cxn_exists, cxn_db, cxn_timestamp, cxn_names
cxn_names = ['trigger_read', 'hlt_rates_write', 'l1_rates_write', 'l1_rate_types_read'] # put in config file later
#temporary
db_usernames = {'trigger_read'       : trigger_db_login,
                'hlt_rates_write'    : hlt_rates_db_login,
                'l1_rates_write'     : l1_rates_db_login,
                'l1_rate_types_read' : l1_rate_type_db_login,
                'file_status'        : file_status_db_login,}
                'file_status_T0'     : file_status_T0_db_login}
db_passwords = {'trigger_read'       : trigger_db_pwd,
                'hlt_rates_write'    : hlt_rates_db_pwd,
                'l1_rates_write'     : l1_rates_db_pwd,
                'l1_rate_types_read' : l1_rate_type_db_pwd,
                'file_status'        : file_status_db_pwd,
                'file_status_T0'     : file_status_T0_db_pwd}
db_sids      = {'trigger_read'       : trigger_db_sid,
                'hlt_rates_write'    : hlt_rates_db_sid,
                'l1_rates_write'     : l1_rates_db_sid,
                'l1_rate_types_read' : l1_rate_type_db_sid,
                'file_status'        : file_status_db_sid,
                'file_status_T0'     : file_status_T0_db_sid}
cxn_exists = {}
cxn_db = {}
cxn_timestamp = {}
for cxn_name in cxn_names:
    cxn_exists[cxn_name]=False
    cxn_timestamp[cxn_name]=0
    cxn_db[cxn_name]=False
    try:
        cxn_db[cxn_name] = cx_Oracle.connect(db_usernames[cxn_name], db_passwords[cxn_name], db_sids[cxn_name])
        cxn_timestamp[cxn_name] = int(time.time())
        cxn_exists[cxn_name] = True
    except cx_Oracle.DatabaseError as e:
        error, = e.args
        logger.error('Error connecting to database "{0}": {1}'.format(cxn_name, returnErrorMessage(error.code)))

def returnErrorMessage(code):
    if code==1017:
        return 'Bad credentials'
    else:
        return 'Unrecognized error ({0})'.format(code)

def useConnection(cxn_name):
    global cxn_exists, cxn_db, cxn_timestamp, cxn_names
    if cxn_name not in cxn_names:
        return False
    fresh_cxn = int(time.time()) - cxn_timestamp[cxn_name] <  cxn_timeout
    if not cxn_exists[cxn_name] or not fresh_cxn:
        if cxn_exists:
            cxn_db[cxn_name].close()
            cxn_exists[cxn_name]=False
            logger.info('Database connection "{0}" has expired. Making a new one...'.format(cxn_name))
        retries=0
        while not cxn_exists[cxn_name] and retries<num_retries:
            try:
                logger.debug('Try #1 to make a new database connection to "{0}"'.format(cxn_name))
                cxn_db[cxn_name] = cx_Oracle.connect(db_usernames[cxn_name], db_passwords[cxn_name], db_sids[cxn_name])
                cxn_timestamp[cxn_name] = int(time.time())
                cxn_exists[cxn_name] = True
                logger.debug('Successfully reconnected to database "{0}"'.format(cxn_name))
            except cx_Oracle.DatabaseError as e:
                error, = e.args
                logger.error('Error connecting to database "{0}". Reason: {1}'.format(cxn_name, returnErrorMessage(error.code)))
                retries+=1
    if not cxn_exists[cxn_name]:
        return False
    else:
        return cxn_db[cxn_name]

class TimeoutError(Exception):
    pass
def timeoutHandler(signum, frame):
    raise TimeoutError()
def timeout(func, args=(), kwargs={}, timeout_duration=1, default=False):
    # set the timeout handler
    signal.signal(signal.SIGALRM, timeoutHandler) # Call timeoutHandler when the signal.SIGALRM is sent
    signal.setitimer(signal.ITIMER_REAL, timeout_duration, 1) # Set interval timer to send a SIGALRM after timeout_duration and every second thereafter
    try:
        result = func(*args, **kwargs)
    except TimeoutError as exc: # Raise exception if SIGALRM signal happens before we receive the query result
        logger.error('Timeout {0} s exceeded calling function {1}'.format(timeout_duration, func.__name__))
        result = default
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0) # Disable the alarm
    return result

def runQuery(cxn_name, query, fetch_output=True, custom_timeout=0):
    # databaseAgent.query
    # Interface for passing queries to the database agent.
    #
    # cxn_name:         slug name for the connection to use
    # query:            SQL statement to run
    # fetch_output:     whether to try to fetch output (will cause an error if you try to fetch from an UPDATE statement)
    # custom_timeout:   override default query timeout, unit is seconds

    logger.debug('Passing a query to database connection "{0}": "{1}"'.format(cxn_name, query.replace('\n', ' ').replace('\r', '')))
    the_cxn = useConnection(cxn_name) # Get a fresh connection object
    if the_cxn==False:
        logger.error('Could not run query, unable to connect to "{0}"'.format(cxn_name))
        return False
    if custom_timeout!=0:
        the_timeout = custom_timeout
    else:
        the_timeout = query_timeout
    args=[the_cxn, query, fetch_output] # Arguments to send to databaseAgent.executeQuery
    ran_query=False
    retries=0
    while ran_query==False and retries<num_retries:
        logger.info('Try #{0} query on database "{1}": "{2}"'.format(retries+1, cxn_name, query))
        try:
            result = timeout(executeQuery, args, timeout_duration=the_timeout, default=False)
            if result != False:
                ran_query=True
        except cx_Oracle.DatabaseError as e:
            error, = e.args
            logger.error('Error running query on database "{0}". Reason: {1}'.format(cxn_name, returnErrorMessage(error.code)))
        except cx_Oracle.InterfaceError as e:
            if e[0]=="not a query":
                logger.error('databaseAgent requested output from non-query SQL statement')
                result=True
                ran_query=True
            else:
                logger.error("databaseAgent unknown Oracle interface error: {0}".format(e[0]))
        retries+=1
    if result==False:
        return False
    else:
        return result
def executeQuery(the_cxn, query, fetch_output=True):
    # Internal function for actually executing the queries
    # Very simple for now but may add some smart stuff later
    cursor=the_cxn.cursor()
    cursor.execute(query)
    if fetch_output:
        result=cursor.fetchall()
    else:
        result=True
    return result
