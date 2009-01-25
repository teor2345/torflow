#!/usr/bin/python
#
# Common code to soat

import dircache
import operator
import os
import pickle
import sys
import time

import sets
from sets import Set

#
# Data storage
#

# data locations

data_dir = './data/soat/'
ssl_certs_dir = data_dir + 'ssl/certs/'
http_tags_dir = data_dir + 'http/tags/'

# constants

TEST_SUCCESS = 0
TEST_INCONCLUSIVE = 1
TEST_FAILURE = 2

# classes to use with pickle to dump test results into files

class TestResult(object):
  ''' Parent class for all test result classes '''
  def __init__(self, exit_node, site, status):
    self.exit_node = exit_node
    self.site = site
    self.timestamp = time.time()
    self.status = status

class SSLTestResult(TestResult):
  ''' Represents the result of an openssl test '''
  def __init__(self, exit_node, ssl_site, cert_file, status):
    super(SSLTestResult, self).__init__(exit_node, ssl_site, status)
    self.cert = cert_file

class HttpTestResult(TestResult):
  ''' Represents the result of a http test '''
  def __init__(self, exit_node, website, tag_prints, status):
    super(HttpTestResult, self).__init__(exit_node, website, status)
    self.tag_prints = tag_prints

class SSHTestResult(TestResult):
  ''' Represents the result of an ssh test '''
  def __init__(self, exit_node, ssh_site, status):
    super(SSHTestResult, self).__init__(exit_node, ssh_site, status)

class DNSTestResult(TestResult):
  ''' Represents the result of a dns test '''
  def __init__(self, exit_node, dns_site, status):
    super(DNSTestResult, self).__init__(exit_node, dns_site, status)

class DNSRebindTestResult(TestResult):
  ''' Represents the result of a dns rebind test '''
  def __init__(self, exit_node, dns_rebind_site, status):
    super(DNSRebindTestResult, self).__init__(exit_node, dns_rebind_site, status)

class SMTPTestResult(TestResult):
  ''' Represents the result of an smtp test '''
  def __init__(self, exit_node, smtp_site, status):
    super(SMTPTestResult, self).__init__(exit_node, smtp_site, status)

class IMAPTestResult(TestResult):
  ''' Represents the result of an imap test '''
  def __init__(self, exit_node, imap_site, status):
    super(IMAPTestResult, self).__init__(exit_node, imap_site, status)

class POPTestResult(TestResult):
  ''' Represents the result of a pop test '''
  def __init__(self, exit_node, pop_site, status):
    super(POPTestResult, self).__init__(exit_node, pop_site, status)

class DataHandler:
  ''' Class for saving and managing test result data '''
  def filterResults(self, results, protocols=[], show_good=False, 
      show_bad=False, show_inconclusive=False):
    ''' filter results based on protocol and success level ''' 

    protocol_filters = []
    status_filters = []

    for protocol in protocols:
      protocol_filters.append(lambda x, p=protocol: x.__class__.__name__.lower()[:-10].endswith(p))
    if show_good:
      status_filters.append(lambda x: x.status == TEST_SUCCESS)
    if show_bad:
      status_filters.append(lambda x: x.status == TEST_FAILURE)
    if show_inconclusive:
      status_filters.append(lambda x: x.status == TEST_INCONCLUSIVE)

    if len(protocol_filters) == 0 or len(status_filters) == 0:
      return []
     
    protocol_filter = lambda x: reduce(operator.__or__, [f(x) for f in protocol_filters])
    status_filter = lambda x: reduce(operator.__or__, [f(x) for f in status_filters])

    return [x for x in results if (protocol_filter(x) and status_filter(x))]
    
  def filterByNode(self, results, id):
    ''' filter by node'''
    return filter(lambda x: x.exit_node == id, results)

  def getAll(self):
    ''' get all available results'''
    return self.__getResults(data_dir)

  def getSsh(self):
    ''' get results of ssh tests '''
    return self.__getResults(data_dir + 'ssh/')
    
  def getHttp(self):
    ''' get results of http tests '''
    return self.__getResults(data_dir + 'http/')

  def getSsl(self):
    ''' get results of ssl tests '''
    return self.__getResults(data_dir + 'ssl/')

  def getSmtp(self):
    ''' get results of smtp tests '''
    return self.__getResults(data_dir + 'smtp/')

  def getPop(self):
    ''' get results of pop tests '''
    return self.__getResults(data_dir + 'pop/')

  def getImap(self):
    ''' get results of imap tests '''
    return self.__getResults(data_dir + 'imap/')

  def getDns(self):
    ''' get results of basic dns tests '''
    return self.__getResults(data_dir + 'dns')

  def getDnsRebind(self):
    ''' get results of dns rebind tests '''
    return self.__getResults(data_dir + 'dnsbrebind/')

  def __getResults(self, dir):
    ''' 
    recursively traverse the directory tree starting with dir
    gather test results from files ending with .result
    '''
    results = []

    for root, dirs, files in os.walk(dir):
      for file in files:
        if file.endswith('result'):
          fh = open(os.path.join(root, file))
          result = pickle.load(fh)
          results.append(result)

    return results

  def safeFilename(self, str):
    ''' 
    remove characters illegal in some systems 
    and trim the string to a reasonable length
    '''
    replaced = (str.replace('/','_').replace('\\','_').replace('?','_').replace(':','_').
      replace('|','_').replace('*','_').replace('<','_').replace('>','_').replace('"',''))
    return replaced[:200]

  def saveResult(self, result):
    ''' generic method for saving test results '''
    address = ''
    if result.__class__.__name__ == 'HttpTestResult':
      address = self.safeFilename(result.site[7:])
    elif result.__class__.__name__ == 'SSLTestResult':
      address = self.safeFilename(result.site[8:])
    elif 'TestResult' in result.__class__.__name__:
      address = self.safeFilename(result.site)
    else:
      raise Exception, 'This doesn\'t seems to be a result instance.'

    dir = data_dir + result.__class__.__name__[:-10].lower() + '/'
    if result.status == TEST_SUCCESS:
      dir += 'successful/'
    if result.status == TEST_INCONCLUSIVE:
      dir += 'inconclusive/'
    if result.status == TEST_FAILURE:
      dir += 'failed/'
    
    result_file = open(dir + result.exit_node[1:] + "-" + address + '.result', 'w')
    pickle.dump(result, result_file)
    result_file.close()

