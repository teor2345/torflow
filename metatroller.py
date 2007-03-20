#!/usr/bin/python
# Metatroller. 

"""
Metatroller - Tor Meta controller
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
from TorCtl import *
from TorCtl.TorUtil import *
from TorCtl.PathSupport import *

mt_version = "0.1.0-dev"

# TODO: Move these to config file
control_host = "127.0.0.1"
control_port = 9061
meta_host = "127.0.0.1"
meta_port = 9052
max_detach = 3

# Do NOT modify this object directly after it is handed to PathBuilder
# Use PathBuilder.schedule_selmgr instead.
# (Modifying the arguments here is OK)
__selmgr = PathSupport.SelectionManager(
      pathlen=3,
      order_exits=False,
      percent_fast=100,
      percent_skip=0,
      min_bw=1024,
      use_all_exits=False,
      uniform=True,
      use_exit=None,
      use_guards=False)

class BandwidthStats:
  def __init__(self):
    self.byte_list = []
    self.duration_list = []
    self.min_bw = 1e10
    self.max_bw = 0
    self.mean = 0
    self.dev = 0

  def _exp(self): # Weighted avg
    tot_bw = reduce(lambda x, y: x+y, self.byte_list, 0.0)
    EX = 0.0
    for i in xrange(len(self.byte_list)):
      EX += (self.byte_list[i]*self.byte_list[i])/self.duration_list[i]
    if tot_bw == 0.0: return 0.0
    EX /= tot_bw
    return EX

  def _exp2(self): # E[X^2]
    tot_bw = reduce(lambda x, y: x+y, self.byte_list, 0.0)
    EX = 0.0
    for i in xrange(len(self.byte_list)):
      EX += (self.byte_list[i]**3)/(self.duration_list[i]**2)
    if tot_bw == 0.0: return 0.0
    EX /= tot_bw
    return EX
    
  def _dev(self): # Weighted dev
    EX = self.mean
    EX2 = self._exp2()
    arg = EX2 - (EX*EX)
    if arg < -0.05:
      plog("WARN", "Diff of "+str(EX2)+" and "+str(EX)+"^2 is "+str(arg))
    return math.sqrt(abs(arg))

  def add_bw(self, bytes, duration):
    if not bytes: plog("WARN", "No bytes for bandwidth")
    bytes /= 1024.
    self.byte_list.append(bytes)
    self.duration_list.append(duration)
    bw = bytes/duration
    plog("DEBUG", "Got bandwidth "+str(bw))
    if self.min_bw > bw: self.min_bw = bw
    if self.max_bw < bw: self.max_bw = bw
    self.mean = self._exp()
    self.dev = self._dev()

# Technically we could just add member vars as we need them, but this
# is a bit more clear
class StatsRouter(TorCtl.Router):
  def __init__(self, router): # Promotion constructor :)
    self.__dict__ = router.__dict__
    self.reset()
  
  def reset(self):
    self.circ_uncounted = 0
    self.circ_failed = 0
    self.circ_succeeded = 0 # disjoint from failed
    self.circ_suspected = 0
    self.circ_chosen = 0 # above 4 should add to this
    self.strm_failed = 0 # Only exits should have these
    self.strm_succeeded = 0
    self.strm_suspected = 0 # disjoint from failed
    self.strm_uncounted = 0
    self.strm_chosen = 0 # above 4 should add to this
    self.reason_suspected = {}
    self.reason_failed = {}
    self.first_seen = time.time()
    if "Running" in self.flags:
      self.became_active_at = self.first_seen
      self.hibernated_at = 0
    else:
      self.became_active_at = 0
      self.hibernated_at = self.first_seen
    self.total_hibernation_time = 0
    self.total_active_uptime = 0
    self.total_extend_time = 0
    self.total_extended = 0
    self.bwstats = BandwidthStats()
    self.z_ratio = 0
    self.prob_zr = 0
    self.z_bw = 0
    self.prob_zb = 0

  def avg_extend_time(self):
    if self.total_extended:
      return self.total_extend_time/self.total_extended
    else: return 0

  def bw_ratio(self):
    bw = self.bwstats.mean
    if bw == 0.0: return 0
    else: return self.bw/(1024.*bw)

  def current_uptime(self):
    if self.became_active_at:
      ret = (self.total_active_uptime+(time.time()-self.became_active_at))
    else:
      ret = self.total_active_uptime
    if ret == 0: return 0.000005 # eh..
    else: return ret
        
  def failed_per_hour(self):
    return (3600.*(self.circ_failed+self.strm_failed))/self.current_uptime()

  # XXX: Seperate suspected from failed in totals 
  def suspected_per_hour(self):
    return (3600.*(self.circ_suspected+self.strm_suspected
          +self.circ_failed+self.strm_failed))/self.current_uptime()

  # These four are for sanity checking
  def _suspected_per_hour(self):
    return (3600.*(self.circ_suspected+self.strm_suspected))/self.current_uptime()

  def _uncounted_per_hour(self):
    return (3600.*(self.circ_uncounted+self.strm_uncounted))/self.current_uptime()

  def _chosen_per_hour(self):
    return (3600.*(self.circ_chosen+self.strm_chosen))/self.current_uptime()

  def _succeeded_per_hour(self):
    return (3600.*(self.circ_succeeded+self.strm_succeeded))/self.current_uptime()
  
  key = """Metatroller Statistics:
  CC=Circuits Chosen   CF=Circuits Failed     CS=Circuit Suspected
  SC=Streams Chosen    SF=Streams Failed      SS=Streams Suspected
  FH=Failed per Hour   SH=Suspected per Hour  ET=avg circuit Extend Time (s)
  EB=mean BW (K)       BD=BW std Dev (K)      BR=Ratio of observed to avg BW
  ZB=BW z-test value   PB=Probability(z-bw)   ZR=Ratio z-test value
  PR=Prob(z-ratio)     U=Uptime (h)\n"""

  def __str__(self):
    return (self.idhex+" ("+self.nickname+")\n"
    +"   CC="+str(self.circ_chosen)
      +" CF="+str(self.circ_failed)
      +" CS="+str(self.circ_suspected+self.circ_failed)
      +" SC="+str(self.strm_chosen)
      +" SF="+str(self.strm_failed)
      +" SS="+str(self.strm_suspected+self.strm_failed)
      +" FH="+str(round(self.failed_per_hour(),1))
      +" SH="+str(round(self.suspected_per_hour(),1))+"\n"
    +"   ET="+str(round(self.avg_extend_time(),1))
      +" EB="+str(round(self.bwstats.mean,1))
      +" BD="+str(round(self.bwstats.dev,1))
      +" ZB="+str(round(self.z_bw,1))
      +" PB="+(str(round(self.prob_zb,3))[1:])
      +" BR="+str(round(self.bw_ratio(),1))
      +" ZR="+str(round(self.z_ratio,1))
      +" PR="+(str(round(self.prob_zr,3))[1:])
      +" U="+str(round(self.current_uptime()/3600, 1))+"\n")

  def sanity_check(self):
    if (self.circ_failed + self.circ_succeeded + self.circ_suspected
      + self.circ_uncounted != self.circ_chosen):
      plog("ERROR", self.nickname+" does not add up for circs")
    if (self.strm_failed + self.strm_succeeded + self.strm_suspected
      + self.strm_uncounted != self.strm_chosen):
      plog("ERROR", self.nickname+" does not add up for streams")
    def check_reasons(reasons, expected, which, rtype):
      count = 0
      for rs in reasons.iterkeys():
        if re.search(r"^"+which, rs): count += reasons[rs]
      if count != expected:
        plog("ERROR", "Mismatch "+which+" "+rtype+" for "+self.nickname)
    check_reasons(self.reason_suspected,self.strm_suspected,"STREAM","susp")
    check_reasons(self.reason_suspected,self.circ_suspected,"CIRC","susp")
    check_reasons(self.reason_failed,self.strm_failed,"STREAM","failed")
    check_reasons(self.reason_failed,self.circ_failed,"CIRC","failed")
    now = time.time()
    tot_hib_time = self.total_hibernation_time
    tot_uptime = self.total_active_uptime
    if self.hibernated_at: tot_hib_time += now - self.hibernated_at
    if self.became_active_at: tot_uptime += now - self.became_active_at
    if round(tot_hib_time+tot_uptime) != round(now-self.first_seen):
      plog("ERROR", "Mismatch of uptimes for "+self.nickname)
    
    per_hour_tot = round(self._uncounted_per_hour()+self.failed_per_hour()+
         self._suspected_per_hour()+self._succeeded_per_hour(), 2)
    chosen_tot = round(self._chosen_per_hour(), 2)
    if per_hour_tot != chosen_tot:
      plog("ERROR", self.nickname+" has mismatch of per hour counts: "
                    +str(per_hour_tot) +" vs "+str(chosen_tot))

class ReasonRouterList:
  "Helper class to track which reasons are in which routers."
  def __init__(self, reason):
    self.reason = reason
    self.rlist = {}

  def sort_list(self): raise NotImplemented()

  def write_list(self, f):
    rlist = self.sort_list()
    for r in rlist:
      susp = 0
      tot_failed = r.circ_failed+r.strm_failed
      tot_susp = tot_failed+r.circ_suspected+r.strm_suspected
      f.write(r.idhex+" ("+r.nickname+") F=")
      if self.reason in r.reason_failed:
        susp = r.reason_failed[self.reason]
      f.write(str(susp)+"/"+str(tot_failed))
      f.write(" S=")
      if self.reason in r.reason_suspected:
        susp += r.reason_suspected[self.reason]
      f.write(str(susp)+"/"+str(tot_susp)+"\n")
    
  def add_r(self, r):
    self.rlist[r] = 1

  def total_suspected(self):
    # suspected is disjoint from failed. The failed table
    # may not have an entry
    def notlambda(x, y):
      if self.reason in y.reason_suspected:
        if self.reason in y.reason_failed:
          return (x + y.reason_suspected[self.reason]
               + y.reason_failed[self.reason])
        else:
          return (x + y.reason_suspected[self.reason])
      else:
        if self.reason in y.reason_failed:
          return (x + y.reason_failed[self.reason])
        else: return x
    return reduce(notlambda, self.rlist.iterkeys(), 0)

  def total_failed(self):
    def notlambda(x, y):
      if self.reason in y.reason_failed:
        return (x + y.reason_failed[self.reason])
      else: return x
    return reduce(notlambda, self.rlist.iterkeys(), 0)
 
class SuspectRouterList(ReasonRouterList):
  def __init__(self, reason): ReasonRouterList.__init__(self,reason)
  
  def sort_list(self):
    rlist = self.rlist.keys()
    rlist.sort(lambda x, y: cmp(y.reason_suspected[self.reason],
                  x.reason_suspected[self.reason]))
    return rlist
   
  def _verify_suspected(self):
    return reduce(lambda x, y: x + y.reason_suspected[self.reason],
            self.rlist.iterkeys(), 0)

class FailedRouterList(ReasonRouterList):
  def __init__(self, reason): ReasonRouterList.__init__(self,reason)

  def sort_list(self):
    rlist = self.rlist.keys()
    rlist.sort(lambda x, y: cmp(y.reason_failed[self.reason],
                  x.reason_failed[self.reason]))
    return rlist

  def _verify_failed(self):
    return reduce(lambda x, y: x + y.reason_failed[self.reason],
            self.rlist.iterkeys(), 0)


class StatsHandler(PathSupport.PathBuilder):
  def __init__(self, c, slmgr):
    PathBuilder.__init__(self, c, slmgr, StatsRouter)
    self.failed_reasons = {}
    self.suspect_reasons = {}

  def run_zbtest(self): # Unweighted z-test
    n = reduce(lambda x, y: x+(y.bwstats.mean > 0), self.sorted_r, 0)
    if n == 0: return (0, 0)
    avg = reduce(lambda x, y: x+y.bwstats.mean, self.sorted_r, 0)/float(n)
    def notlambda(x, y):
      if y.bwstats.mean <= 0: return x+0
      else: return x+(y.bwstats.mean-avg)*(y.bwstats.mean-avg)
    stddev = math.sqrt(reduce(notlambda, self.sorted_r, 0)/float(n))
    if not stddev: return (avg, stddev)
    for r in self.sorted_r:
      if r.bwstats.mean > 0:
        r.z_bw = abs((r.bwstats.mean-avg)/stddev)
        r.prob_zb = TorUtil.zprob(-r.z_bw)
    return (avg, stddev)

  def run_zrtest(self): # Unweighted z-test
    n = reduce(lambda x, y: x+(y.bw_ratio() > 0), self.sorted_r, 0)
    if n == 0: return (0, 0)
    avg = reduce(lambda x, y: x+y.bw_ratio(), self.sorted_r, 0)/float(n)
    def notlambda(x, y):
      if y.bw_ratio() <= 0: return x+0
      else: return x+(y.bw_ratio()-avg)*(y.bw_ratio()-avg)
    stddev = math.sqrt(reduce(notlambda, self.sorted_r, 0)/float(n))
    if not stddev: return (avg, stddev)
    for r in self.sorted_r:
      if r.bw_ratio() > 0:
        r.z_ratio = abs((r.bw_ratio()-avg)/stddev)
        r.prob_zr = TorUtil.zprob(-r.z_ratio)
    return (avg, stddev)

  def write_reasons(self, f, reasons, name):
    f.write("\n\n\t----------------- "+name+" -----------------\n")
    for rsn in reasons:
      f.write("\n"+rsn.reason+". Failed: "+str(rsn.total_failed())
          +", Suspected: "+str(rsn.total_suspected())+"\n")
      rsn.write_list(f)

  def write_routers(self, f, rlist, name):
    f.write("\n\n\t----------------- "+name+" -----------------\n\n")
    for r in rlist:
      # only print it if we've used it.
      if r.circ_chosen+r.strm_chosen > 0: f.write(str(r))

  def write_stats(self, filename):
    # TODO: all this shit should be configurable. Some of it only makes
    # sense when scanning in certain modes.
    plog("DEBUG", "Writing stats")
    # Sanity check routers
    for r in self.sorted_r: r.sanity_check()

    # Sanity check the router reason lists.
    for r in self.sorted_r:
      for rsn in r.reason_failed:
        if r not in self.failed_reasons[rsn].rlist:
          plog("ERROR", "Router missing from reason table")
      for rsn in r.reason_suspected:
        if r not in self.suspect_reasons[rsn].rlist:
          plog("ERROR", "Router missing from reason table")

    # Sanity check the lists the other way
    for rsn in self.failed_reasons.itervalues(): rsn._verify_failed()
    for rsn in self.suspect_reasons.itervalues(): rsn._verify_suspected()

    f = file(filename, "w")
    f.write(StatsRouter.key)
    (avg, dev) = self.run_zbtest()
    f.write("\n\nBW stats: u="+str(round(avg,1))+" s="+str(round(dev,1))+"\n")

    (avg, dev) = self.run_zrtest()
    f.write("BW ratio stats: u="+str(round(avg,1)) +" s="+str(round(dev,1)))

    # sort+print by bandwidth
    bw_rate = copy.copy(self.sorted_r)
    bw_rate.sort(lambda x, y: cmp(y.bw_ratio(), x.bw_ratio()))
    self.write_routers(f, bw_rate, "Bandwidth Ratios")

    failed = copy.copy(self.sorted_r)
    failed.sort(lambda x, y:
          cmp(y.circ_failed+y.strm_failed,
            x.circ_failed+x.strm_failed))
    self.write_routers(f, failed, "Failed Counts")

    suspected = copy.copy(self.sorted_r)
    suspected.sort(lambda x, y: # Suspected includes failed
       cmp(y.circ_failed+y.strm_failed+y.circ_suspected+y.strm_suspected,
         x.circ_failed+x.strm_failed+x.circ_suspected+x.strm_suspected))
    self.write_routers(f, suspected, "Suspected Counts")

    fail_rate = copy.copy(failed)
    fail_rate.sort(lambda x, y: cmp(y.failed_per_hour(), x.failed_per_hour()))
    self.write_routers(f, fail_rate, "Fail Rates")

    suspect_rate = copy.copy(suspected)
    suspect_rate.sort(lambda x, y:
       cmp(y.suspected_per_hour(), x.suspected_per_hour()))
    self.write_routers(f, suspect_rate, "Suspect Rates")
    
    # TODO: Sort by failed/selected and suspect/selected ratios
    # if we ever want to do non-uniform scanning..

    # FIXME: Add failed in here somehow..
    susp_reasons = self.suspect_reasons.values()
    susp_reasons.sort(lambda x, y:
       cmp(y.total_suspected(), x.total_suspected()))
    self.write_reasons(f, susp_reasons, "Suspect Reasons")

    fail_reasons = self.failed_reasons.values()
    fail_reasons.sort(lambda x, y:
       cmp(y.total_failed(), x.total_failed()))
    self.write_reasons(f, fail_reasons, "Failed Reasons")
    f.close()

    # FIXME: sort+print by circ extend time

  def reset_stats(self):
    plog("DEBUG", "Resetting stats")
    self.suspect_reasons.clear()
    self.failed_reasons.clear()
    for r in self.sorted_r: r.reset()

  def circ_status_event(self, c):
    if c.circ_id in self.circuits:
      # TODO: Hrmm, consider making this sane in TorCtl.
      if c.reason: lreason = c.reason
      else: lreason = "NONE"
      if c.remote_reason: rreason = c.remote_reason
      else: rreason = "NONE"
      reason = c.event_name+":"+c.status+":"+lreason+":"+rreason
      if c.status == "EXTENDED":
        delta = c.arrived_at - self.circuits[c.circ_id].last_extended_at
        r_ext = c.path[-1]
        if r_ext[0] != '$': r_ext = self.name_to_key[r_ext]
        self.routers[r_ext[1:]].total_extend_time += delta
        self.routers[r_ext[1:]].total_extended += 1
      elif c.status == "FAILED":
        # update selection count
        for r in self.circuits[c.circ_id].path: r.circ_chosen += 1
        
        if len(c.path)-1 < 0: start_f = 0
        else: start_f = len(c.path)-1 

        # Count failed
        # XXX: Differentiate between extender and extendee
        for r in self.circuits[c.circ_id].path[start_f:len(c.path)+1]:
          r.circ_failed += 1
          if not reason in r.reason_failed:
            r.reason_failed[reason] = 1
          else: r.reason_failed[reason]+=1
          if reason not in self.failed_reasons:
             self.failed_reasons[reason] = FailedRouterList(reason)
          self.failed_reasons[reason].add_r(r)

        for r in self.circuits[c.circ_id].path[len(c.path)+1:]:
          r.circ_uncounted += 1

        # Don't count if failed was set this round, don't set 
        # suspected..
        for r in self.circuits[c.circ_id].path[:start_f]:
          r.circ_suspected += 1
          if not reason in r.reason_suspected:
            r.reason_suspected[reason] = 1
          else: r.reason_suspected[reason]+=1
          if reason not in self.suspect_reasons:
             self.suspect_reasons[reason] = SuspectRouterList(reason)
          self.suspect_reasons[reason].add_r(r)
      elif c.status == "CLOSED":
        # Since PathBuilder deletes the circuit on a failed, 
        # we only get this for a clean close
        # Update circ_chosen count
        for r in self.circuits[c.circ_id].path:
          r.circ_chosen += 1
        
          if lreason in ("REQUESTED", "FINISHED", "ORIGIN"):
            r.circ_succeeded += 1
          else:
            if not reason in r.reason_suspected:
              r.reason_suspected[reason] = 1
            else: r.reason_suspected[reason] += 1
            r.circ_suspected+= 1
            if reason not in self.suspect_reasons:
              self.suspect_reasons[reason] = SuspectRouterList(reason)
            self.suspect_reasons[reason].add_r(r)
    PathBuilder.circ_status_event(self, c)

  def count_stream_reason_failed(self, s, reason):
    # Update failed count,reason_failed for exit
    r = self.circuits[s.circ_id].exit
    if not reason in r.reason_failed: r.reason_failed[reason] = 1
    else: r.reason_failed[reason]+=1
    r.strm_failed += 1
    if reason not in self.failed_reasons:
      self.failed_reasons[reason] = FailedRouterList(reason)
    self.failed_reasons[reason].add_r(r)

  def count_stream_suspects(self, s, lreason, reason):
    if lreason in ("TIMEOUT", "INTERNAL", "TORPROTOCOL" "DESTROY"):
      for r in self.circuits[s.circ_id].path[:-1]:
        r.strm_suspected += 1
        if not reason in r.reason_suspected:
          r.reason_suspected[reason] = 1
        else: r.reason_suspected[reason]+=1
        if reason not in self.suspect_reasons:
          self.suspect_reasons[reason] = SuspectRouterList(reason)
        self.suspect_reasons[reason].add_r(r)
    else:
      for r in self.circuits[s.circ_id].path[:-1]:
        r.strm_uncounted += 1
  
  def stream_status_event(self, s):
    if s.strm_id in self.streams:
      # TODO: Hrmm, consider making this sane in TorCtl.
      if s.reason: lreason = s.reason
      else: lreason = "NONE"
      if s.remote_reason: rreason = s.remote_reason
      else: rreason = "NONE"
      reason = s.event_name+":"+s.status+":"+lreason+":"+rreason+":"+self.streams[s.strm_id].kind
      if (s.status in ("DETACHED", "FAILED", "CLOSED", "SUCCEEDED")
          and not s.circ_id):
        # XXX: REMAPs can do this (normal). Also REASON=DESTROY (bug?)
        # Also timeouts.. Those should use the pending circ instead
        # of returning..
        plog("WARN", "Stream "+str(s.strm_id)+" detached from no circuit!")
        PathBuilder.stream_status_event(self, s)
        return

      # Verify circ id matches stream.circ
      if s.status not in ("NEW", "NEWRESOLVE", "REMAP"):
        circ = self.streams[s.strm_id].circ
        if not circ: circ = self.streams[s.strm_id].pending_circ
        if circ and circ.circ_id != s.circ_id:
          plog("WARN", str(s.strm_id) + " has mismatch of "
                +str(s.circ_id)+" v "+str(circ.circ_id))
      
      if s.status == "DETACHED":
        if self.streams[s.strm_id].attached_at:
          plog("WARN", str(s.strm_id)+" detached after succeeded")
        # Update strm_chosen count
        for r in self.circuits[s.circ_id].path: r.strm_chosen += 1
        self.count_stream_suspects(s, lreason, reason)
        self.count_stream_reason_failed(s, reason)
      elif s.status == "FAILED":
        # HACK. We get both failed and closed for the same stream,
        # with different reasons. Might as well record both, since they 
        # often differ.
        self.streams[s.strm_id].failed_reason = reason
      elif s.status == "CLOSED":
        # Always get both a closed and a failed.. 
        #   - Check if the circuit exists still
        # Update strm_chosen count
        for r in self.circuits[s.circ_id].path: r.strm_chosen += 1

        # Update bw stats. XXX: Don't do this for resolve streams
        if self.streams[s.strm_id].attached_at:
          lifespan = self.streams[s.strm_id].lifespan(s.arrived_at)
          for r in self.streams[s.strm_id].circ.path:
            r.bwstats.add_bw(self.streams[s.strm_id].bytes_written+
                             self.streams[s.strm_id].bytes_read, lifespan)

        if self.streams[s.strm_id].failed:
          reason = self.streams[s.strm_id].failed_reason+":"+lreason+":"+rreason

        self.count_stream_suspects(s, lreason, reason)
          
        r = self.circuits[s.circ_id].exit
        if (not self.streams[s.strm_id].failed
          and (lreason == "DONE" or (lreason == "END" and rreason == "DONE"))):
          r.strm_succeeded += 1
        else:
          self.count_stream_reason_failed(s, reason)
    PathBuilder.stream_status_event(self, s)

  def ns_event(self, n):
    PathBuilder.ns_event(self, n)
    now = n.arrived_at
    for ns in n.nslist:
      if not ns.idhex in self.routers:
        continue
      r = self.routers[ns.idhex]
      if "Running" in ns.flags:
        if not r.became_active_at:
          r.became_active_at = now
          r.total_hibernation_time += now - r.hibernated_at
        r.hibernated_at = 0
      else:
        if not r.hibernated_at:
          r.hibernated_at = now
          r.total_active_uptime += now - r.became_active_at
        r.became_active_at = 0
        

def clear_dns_cache(c):
  lines = c.sendAndRecv("SIGNAL CLEARDNSCACHE\r\n")
  for _,msg,more in lines:
    plog("DEBUG", msg)
 
def commandloop(s, c, h):
  s.write("220 Welcome to the Tor Metatroller "+mt_version+"! Try HELP for Info\r\n\r\n")
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
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "USEALLEXITS":
      try:
        if arg:
          use_all_exits = int(arg)
          def notlambda(sm): sm.use_all_exits=use_all_exits
          h.schedule_selmgr(notlambda)
        s.write("250 USEALLEXITS="+str(use_all_exits)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "PRECIRCUITS":
      try:
        if arg:
          num_circuits = int(arg)
          def notlambda(pb): pb.num_circuits=num_circuits
          h.schedule_immediate(notlambda)
        s.write("250 PRECIRCUITS="+str(num_circuits)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "RESOLVEPORT":
      try:
        if arg:
          resolve_port = int(arg)
          def notlambda(pb): pb.resolve_port=resolve_port
          h.schedule_immediate(notlambda)
        s.write("250 RESOLVEPORT="+str(resolve_port)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "PERCENTFAST":
      try:
        if arg:
          percent_fast = int(arg)
          def notlambda(sm): sm.percent_fast=percent_fast
          h.schedule_selmgr(notlambda)
        s.write("250 PERCENTFAST="+str(percent_fast)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "PERCENTSKIP":
      try:
        if arg:
          percent_skip = int(arg)
          def notlambda(sm): sm.percent_skip=percent_skip
          h.schedule_selmgr(notlambda)
        s.write("250 PERCENTSKIP="+str(percent_skip)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "BWCUTOFF":
      try:
        if arg:
          min_bw = int(arg)
          def notlambda(sm): sm.min_bw=min_bw
          h.schedule_selmgr(notlambda)
        s.write("250 BWCUTOFF="+str(min_bw)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "UNIFORM":
      s.write("250 OK\r\n")
    elif command == "PATHLEN":
      try:
        if arg:
          pathlen = int(arg)
          # Technically this doesn't need a full selmgr update.. But
          # the user shouldn't be changing it very often..
          def notlambda(sm): sm.pathlen=pathlen
          h.schedule_selmgr(notlambda)
        s.write("250 PATHLEN="+str(pathlen)+" OK\r\n")
      except ValueError:
        s.write("510 Integer expected\r\n")
    elif command == "SETEXIT":
      if arg:
        # FIXME: Hrmm.. if teh user is a dumbass this will fail silently
        def notlambda(sm): sm.exit_name=arg
        h.schedule_selmgr(notlambda)
        s.write("250 OK\r\n")
      else:
        s.write("510 Argument expected\r\n")
    elif command == "GUARDNODES":
      s.write("250 OK\r\n")
    elif command == "SAVESTATS":
      if arg: filename = arg
      else: filename = "./data/stats-"+time.strftime("20%y-%m-%d-%H:%M:%S")
      def notlambda(this): this.write_stats(filename)
      h.schedule_low_prio(notlambda)
      s.write("250 OK\r\n")
    elif command == "RESETSTATS":
      plog("DEBUG", "Got resetstats")
      def notlambda(this): this.reset_stats()
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

def cleanup(c, s):
  c.set_option("__LeaveStreamsUnattached", "0")
  s.close()

def listenloop(c, h):
  """Loop that handles metatroller commands"""
  srv = ListenSocket(meta_host, meta_port)
  atexit.register(cleanup, *(c, srv))
  while 1:
    client = srv.accept()
    if not client: break
    thr = threading.Thread(None, lambda: commandloop(BufSock(client), c, h))
    thr.start()
  srv.close()

def startup():
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect((control_host,control_port))
  c = PathSupport.Connection(s)
  c.debug(file("control.log", "w"))
  c.authenticate()
  h = StatsHandler(c, __selmgr)
  c.set_event_handler(h)
  c.set_events([TorCtl.EVENT_TYPE.STREAM,
          TorCtl.EVENT_TYPE.BW,
          TorCtl.EVENT_TYPE.NS,
          TorCtl.EVENT_TYPE.CIRC,
          TorCtl.EVENT_TYPE.STREAM_BW,
          TorCtl.EVENT_TYPE.NEWDESC], True)
  c.set_option("__LeaveStreamsUnattached", "1")
  return (c,h)

def main(argv):
  listenloop(*startup())

if __name__ == '__main__':
  main(sys.argv)
