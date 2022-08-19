import argparse
import datetime
import ipaddress
import random
import sqlite3
import statistics
import time

import icmplib

DEFAULT_DB="latency.db"
class PingDB:
    def __init__(self, dbfile=None):
        # The DB consists of two tables: one a list of IPs with JSON metadata, and the other a list of latency histories
        if not dbfile:
            self.dbfile=DEFAULT_DB
        else:
            self.dbfile=dbfile

        self.con = sqlite3.connect(self.dbfile) # Own connection for the life of the object
        with self.con:
            self.con.execute("CREATE TABLE IF NOT EXISTS destinations (ip INTEGER PRIMARY KEY, metadata TEXT);")
            self.con.execute("CREATE TABLE IF NOT EXISTS pings (ts TIMESTAMP, ip INTEGER, ttl INTEGER, latency REAL, is_alive BOOLEAN);")

    def add_ips(self, ips, metadata):
        """
        Accept an individual IP or subnet and adds to the tracking list.
        """
        ips = ipaddress.ip_network(ips,strict=False)
        for ip in ips.hosts():
            int_ip = int(ipaddress.ip_address(ip))
            try:
                with self.con:
                    self.con.execute("INSERT INTO destinations VALUES(?, ?)", (int_ip, metadata))
            except sqlite3.IntegrityError:
                print("Skipping %s, already tracked." % ip)

    def get_tracked_ips(self):
        with self.con:
            res = self.con.execute("SELECT ip FROM destinations").fetchall()
        ips = []
        for r in res:
            ips.append(ipaddress.ip_address(r[0]))
        return ips

    def reset_ips(self):
        """
        Remove all IPs from the tracking list. Does not remove ping data.
        """
        with self.con:
            self.con.execute("DELETE FROM destinations;")

    def add_ping_record(self, timestamp, ip, ttl, latency, is_alive):
        """
        Add a ping record.

        Parameters:
            ts: A datatime (in UTC) when this measurement was taken
            ip: The destination IP of the ping
            ttl: The TTL of the response
            latency: Latency, in millisconds
        """
        int_ip = int(ipaddress.ip_address(ip))
        ts = timestamp.timestamp() * 1000
        with self.con:
            self.con.execute("INSERT INTO pings VALUES(?, ?, ?, ?, ?)", (ts, int_ip, ttl, latency, is_alive))

    def show_stats(self, display=True, outfile=None):
        """
        Show basic stats about the ping history.

        For each IP, compute:
            - Total pings
            - Min/Max/Median/95th %tile RTT
            - % of pings that receive response

        If display=True, print the results.
        If outfile is not None, write the results to a CSV file with the given name.
        """
        results = self.gather_stats()
        if display:
            print("IP,total_pings,min_rtt,max_rtt,median_rtt,95th_rtt,total_alive")
            for ip in results:
                r = results[ip]
                print("%s,%d,%0.2f,%0.2f,%0.2f,%0.2f,%d" % (str(ip), r["total_pings"], r["min_rtt"], r["max_rtt"], r["median_rtt"], r["95th_rtt"], r["total_alive"]))

        if outfile:
            with open(outfile, "w") as output_file:
                output_file.write("IP,total_pings,min_rtt,max_rtt,median_rtt,95th_rtt,total_alive\n")
                for ip in results:
                    output_file.write("%s,%d,%0.2f,%0.2f,%0.2f,%0.2f,%d\n" % (str(ip), r["total_pings"], r["min_rtt"], r["max_rtt"], r["median_rtt"], r["95th_rtt"], r["total_alive"]))

    def gather_stats(self):
        results = {}
        with self.con:
            # First, get all the IPs we track
            res = self.con.execute("SELECT DISTINCT ip FROM pings;")
            int_ips = [_[0] for _ in res.fetchall()]

            # Then, for each IP...
            for int_ip in int_ips:
                ip = ipaddress.ip_address(int_ip)
                results[ip] = {"total_pings": None, "min_rtt": None, "max_rtt": None, "median_rtt": None, "95th_rtt": None, "total_alive": None}

                res = self.con.execute("SELECT COUNT(ip) FROM pings WHERE ip=?", (int_ip,))
                results[ip]['total_pings'] = int(res.fetchone()[0])

                res = self.con.execute("SELECT COUNT(ip) FROM pings WHERE ip=? AND is_alive=1", (int_ip,))
                results[ip]['total_alive'] = int(res.fetchone()[0])

                res = self.con.execute("SELECT latency FROM pings where ip=? AND is_alive=1", (int_ip,))
                latencies = [float(_[0]) for _ in res.fetchall()]
                if len(latencies) == 0: # this was never alive!
                    results[ip]['min_rtt'] = -1.
                    results[ip]['max_rtt'] = -1.
                    results[ip]['median_rtt'] = -1.
                    results[ip]['95th_rtt'] = -1.
                else:
                    results[ip]['min_rtt'] = min(latencies)
                    results[ip]['max_rtt'] = max(latencies)
                    results[ip]['median_rtt'] = statistics.median(latencies)
                    percentiles = statistics.quantiles(latencies,n=100,method='inclusive')
                    results[ip]['95th_rtt'] = percentiles[94]

        return results




class Prober:
    """
    Variables for prober behavior:
        - Randomize or in-order pinging of hosts (default: random)
        - Size of ping blocks (default: 10)
        - Minimum time between rounds of pings (default: 5s)
        - Wait time after pinging all addresses (default: 15min)
    """
    def __init__(self, randomize, parallel, chunk_wait, round_wait, dbfile=None):
        if not dbfile:
            self.dbfile=DEFAULT_DB
        else:
            self.dbfile=dbfile

        self.pdb = PingDB(self.dbfile)
        self.ips = self.pdb.get_tracked_ips()

        self.randomize_order = randomize
        self.num_parallel_pings = parallel # IPs at a time
        self.time_between_chunks = chunk_wait # seconds
        self.time_between_rounds = round_wait # seconds


    @property
    def feasible(self):
        return self.time_between_rounds > (len(self.ips)/self.num_parallel_pings*self.time_between_chunks)

    def runloop(self):
        try:
            while True:
                # Update the list of IPs to track
                self.ips = self.pdb.get_tracked_ips()
                ips_to_ping = self.ips[:]
                if self.randomize_order:
                    random.shuffle(ips_to_ping)

                ## Ping IPs
                for i in range(0, len(ips_to_ping), self.num_parallel_pings):
                    chunk = ips_to_ping[i:i+self.num_parallel_pings]
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    hosts = icmplib.multiping([str(ipaddress.ip_address(ip)) for ip in chunk],
                                      count=1, timeout=1, privileged=False)
                    for host in hosts:
                        self.pdb.add_ping_record(now_utc,host.address,-1,host.max_rtt,host.is_alive)
                    time.sleep(self.time_between_chunks) # sleep time between chunks

            # All IPs have been pinged, wait the time between rounds before trying again
            time.sleep(self.time_between_rounds)
        except KeyboardInterrupt:
            pass
