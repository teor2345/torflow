#!/usr/bin/python
# Metatroller. 

"""
Tor Meta controller

The Metatroller uses TorCtl.PathSupport to build a meta-controller that
listens for commands on a local TCP port. In addition, it gathers a
large amount of statistics on circuit failure rates, streams failure
rates, stream bandwidths, probabilities of bandwidth ratios, and much
much more.

It is also a good place to start for hacking in your own custom
path selection policy. You can remove the SelectionManager it uses
and replace it with your own BaseSelectionManager implementation, 
potentially removing the command loop/meta control port code as
needed.

"""

import atexit
import sys
import socket
import traceback
import re
import random
import threading
import struct
import copy
import time
import math
#from TorCtl import *

from TorCtl import TorUtil, PathSupport, TorCtl #, StatsSupport
from TorCtl.TorUtil import *
from TorCtl.PathSupport import *
from TorCtl.TorUtil import meta_port, meta_host, control_port, control_host, control_pass
#from TorCtl.StatsSupport import StatsHandler,StatsRouter

mt_version = "0.1.0-dev"
max_detach = 3

# Do NOT modify this object directly after it is handed to PathBuilder
# Use PathBuilder.schedule_selmgr instead.
# (Modifying the arguments here is OK)
# NOTE: Custom implementations may wish to replace this with their
# own PathSupport.BaseSelectionManager implementation
__selmgr = PathSupport.SelectionManager(
      pathlen=3,
      order_exits=True,
      percent_fast=80,
      percent_skip=0,
      min_bw=1024,
      use_all_exits=True,
      uniform=True,
      use_exit=None,
      use_guards=True)


def clear_dns_cache(c):
  lines = c.sendAndRecv("SIGNAL CLEARDNSCACHE\r\n")
  for _,msg,more in lines:
    plog("DEBUG", msg)
 
def commandloop(s, c, h):
  "The main metatroller listener loop"
  s.write("220 Welcome to the Tor Metatroller "+mt_version+"! Try HELP for Info\r\n\r\n")

  percent_skip=__selmgr.percent_skip
  percent_fast=__selmgr.percent_fast
  while 1:
    buf = s.readline()
    if not buf: break
    
    m = re.search(r"^(\S+)(?:\s(\S+))?", buf)
    if not m:
      s.write("500 "+buf+" is not a metatroller command\r\n")
      continue
    (command, arg) = m.groups()
    if command == "GETLASTEXIT":
      # local assignment avoids need for lock w/ GIL
      # http://effbot.org/pyfaq/can-t-we-get-rid-of-the-global-interpreter-lock.htm
      # http://effbot.org/pyfaq/what-kinds-of-global-value-mutation-are-thread-safe.htm
      le = h.last_exit
      if le:
        s.write("250 LASTEXIT=$"+le.idhex+" ("+le.nickname+") OK\r\n")
      else:
        s.write("250 LASTEXIT=0 (0) OK\r\n")
    elif command == "NEWEXIT" or command == "NEWNYM":
      # XXX: Seperate this
      clear_dns_cache(c)
      h.new_nym = True # GIL hack
      plog("DEBUG", "Got new nym")
      s.write("250 NEWNYM OK\r\n")
    elif command == "GETDNSEXIT":
      pass # TODO: Takes a hostname? Or prints most recent?
    elif command == "ORDEREXITS":
      try:
        if arg:
          order_exits = int(arg)
          def notlambda(sm): sm.order_exits=order_exits
          h.schedule_selmgr(notlambda)
          s.write("250 ORDEREXITS="+str(order_exits)+" OK\r\n")
        else:
          s.write("250 ORDEREXITS="+str(h.selmgr.order_exits)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "USEALLEXITS":
      try:
        if arg:
          use_all_exits = int(arg)
          def notlambda(sm): sm.use_all_exits=use_all_exits
          h.schedule_selmgr(notlambda)
          s.write("250 USEALLEXITS="+str(use_all_exits)+" OK\r\n")
        else:
          s.write("250 USEALLEXITS="+str(h.selmgr.use_all_exits)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "PRECIRCUITS": # XXX: Use op-addon code for this..
      try:
        if arg:
          num_circuits = int(arg)
          def notlambda(pb): pb.num_circuits=num_circuits
          h.schedule_immediate(notlambda)
          s.write("250 PRECIRCUITS="+str(num_circuits)+" OK\r\n")
        else:
          s.write("250 PRECIRCUITS="+str(h.num_circuits)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "RESOLVEPORT":
      try:
        if arg:
          resolve_port = int(arg)
          def notlambda(pb): pb.resolve_port=resolve_port
          h.schedule_immediate(notlambda)
          s.write("250 RESOLVEPORT="+str(resolve_port)+" OK\r\n")
        else:
          s.write("250 RESOLVEPORT="+str(h.resolve_port)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "PERCENTFAST":
      try:
        if arg:
          percent_fast = int(arg)
          def notlambda(sm): sm.percent_fast=percent_fast
          h.schedule_selmgr(notlambda)
          s.write("250 PERCENTFAST="+str(percent_fast)+" OK\r\n")
        else:
          s.write("250 PERCENTFAST="+str(h.selmgr.percent_fast)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "PERCENTSKIP":
      try:
        if arg:
          percent_skip = int(arg)
          def notlambda(sm): sm.percent_skip=percent_skip
          h.schedule_selmgr(notlambda)
          s.write("250 PERCENTSKIP="+str(percent_skip)+" OK\r\n")
        else:
          s.write("250 PERCENTSKIP="+str(h.selmgr.percent_skip)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "BWCUTOFF":
      try:
        if arg:
          min_bw = int(arg)
          def notlambda(sm): sm.min_bw=min_bw
          h.schedule_selmgr(notlambda)
          s.write("250 BWCUTOFF="+str(min_bw)+" OK\r\n")
        else:
          s.write("250 BWCUTOFF="+str(h.selmgr.min_bw)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "UNIFORM":
      try:
        if arg:
          uniform = int(arg)
          def notlambda(sm): sm.uniform=uniform
          h.schedule_selmgr(notlambda)
          s.write("250 UNIFORM="+str(uniform)+" OK\r\n")
        else:
          s.write("250 UNIFORM="+str(h.selmgr.uniform)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "PATHLEN":
      try:
        if arg:
          pathlen = int(arg)
          # Technically this doesn't need a full selmgr update.. But
          # the user shouldn't be changing it very often..
          def notlambda(sm): sm.pathlen=pathlen
          h.schedule_selmgr(notlambda)
          s.write("250 PATHLEN="+str(pathlen)+" OK\r\n")
        else:
          s.write("250 PATHLEN="+str(h.selmgr.pathlen)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "SETEXIT":
      if arg:
        exit_name = arg
        plog("DEBUG", "Got Setexit: "+exit_name)
        def notlambda(sm): 
          plog("DEBUG", "Job for setexit: "+exit_name)
          sm.set_exit(exit_name)
        h.schedule_selmgr(notlambda)
        s.write("250 OK\r\n")
      else:
        s.write("510 Argument expected\r\n")
    elif command == "GUARDNODES":
      try:
        if arg:
          use_guards = bool(int(arg))
          plog("DEBUG", "Got Setexit: "+str(use_guards))
          def notlambda(sm): 
            plog("DEBUG", "Job for setexit: "+str(use_guards))
            sm.use_guards = use_guards
          h.schedule_selmgr(notlambda)
        s.write("250 OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "SQLSUPPORT":
     try:
        if arg:
          plog("DEBUG", "Got sqlite: "+arg)
          use_db = arg
          from TorCtl import SQLSupport
          SQLSupport.setup_db(use_db, echo=False, drop=True)
          h.add_event_listener(SQLSupport.ConsensusTrackerListener())
          h.add_event_listener(SQLSupport.StreamListener())
          plog("DEBUG", "Did sqlite: "+arg)
          s.write("250 OK\r\n")
     except ValueError:
       s.write("510 database expected\r\n")
    elif command == "CLOSEALLCIRCS":
      def notlambda(this): this.close_all_circuits()
      h.schedule_immediate(notlambda)
      s.write("250 OK\r\n")
    elif command == "SAVESTATS":
      if arg: filename = arg
      else: filename="./data/stats/stats-"+time.strftime("20%y-%m-%d-%H:%M:%S")
      def notlambda(this): this.write_stats(filename)
      h.schedule_low_prio(notlambda)
      s.write("250 OK\r\n")
    elif command == "SAVERATIOS":
      if arg: rfilename = arg
      else: rfilename="./data/stats/ratios-"+time.strftime("20%y-%m-%d-%H:%M:%S")
      def notlambda(this): this.write_ratios(rfilename)
      h.schedule_low_prio(notlambda)
      s.write("250 OK\r\n")
    elif command == "SAVESQL":
      # TODO: Use threading conditions more. Maybe even get some
      # better status reporting than always blindly printing OK.

      if arg: rfilename = arg
      else: rfilename="./data/stats/sql-"+time.strftime("20%y-%m-%d-%H:%M:%S")
      cond = threading.Condition() 
      def notlambda(h):
        cond.acquire()
        SQLSupport.RouterStats.write_stats(file(rfilename, "w"),
                             percent_skip, percent_fast, 
                              order_by=SQLSupport.RouterStats.sbw,
                              recompute=True)
        cond.notify()
        cond.release()
      cond.acquire()
      h.schedule_low_prio(notlambda)
      cond.wait()
      cond.release()
      s.write("250 OK\r\n")
    elif command == "RESETSTATS":
      plog("DEBUG", "Got resetstats")
      def notlambda(this): this.reset()
      h.schedule_low_prio(notlambda)
      s.write("250 OK\r\n")
    elif command == "COMMIT":
      plog("DEBUG", "Got commit")
      def notlambda(this): this.run_all_jobs = True
      h.schedule_immediate(notlambda)
      s.write("250 OK\r\n")
    elif command == "HELP":
      s.write("250 OK\r\n")
       
    else:
      s.write("500 "+buf+" is not a metatroller command\r\n")
  s.close()

def cleanup(c, s, f):
  plog("INFO", "Resetting __LeaveStreamsUnattached=0 and FetchUselessDescriptors="+f)
  try:
    c.set_option("__LeaveStreamsUnattached", "0")
    c.set_option("FetchUselessDescriptors", f)
  except TorCtl.TorCtlClosed:
    pass
  s.close()

def listenloop(c, h, f):
  """Loop that handles metatroller commands"""
  srv = ListenSocket(meta_host, meta_port)
  atexit.register(cleanup, *(c, srv, f))
  while 1:
    client = srv.accept()
    if not client: break
    thr = threading.Thread(None, lambda: commandloop(BufSock(client), c, h))
    thr.start()
  srv.close()

def startup():
  c = TorCtl.connect(control_host, control_port, ConnClass=PathSupport.Connection)
  c.debug(file("control.log", "w", buffering=0))
  h = PathSupport.PathBuilder(c, __selmgr) # StatsHandler(c, __selmgr)

  c.set_event_handler(h)

  c.set_events([TorCtl.EVENT_TYPE.STREAM,
          TorCtl.EVENT_TYPE.BW,
          TorCtl.EVENT_TYPE.NEWCONSENSUS,
          TorCtl.EVENT_TYPE.NEWDESC,
          TorCtl.EVENT_TYPE.CIRC,
          TorCtl.EVENT_TYPE.STREAM_BW], True)
  c.set_option("__LeaveStreamsUnattached", "1") 
  f = c.get_option("FetchUselessDescriptors")[0][1]
  c.set_option("FetchUselessDescriptors", "1") 

  return (c,h,f)

def main(argv):
  listenloop(*startup())

if __name__ == '__main__':
  main(sys.argv)
