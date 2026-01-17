import socket
import threading
import time
import uuid
import hashlib
import sys
import argparse

# CONFIGURATION PARAMETERS

GROUP_PORT = 50000           # UDP port used by all peers for communication

# Discovery configuration
HELLO_INTERVAL = 2.0         # Seconds between HELLO broadcast messages
PEER_TIMEOUT = 6.0           # Seconds after which a peer is considered offline

# Leader election configuration
HB_INTERVAL = 2.0            # Seconds between leader heartbeat broadcasts
LEADER_TIMEOUT = 6.0         # Seconds without heartbeat before triggering election

# Reliable delivery configuration
RETX_INTERVAL = 0.5          # Seconds between retransmission check cycles
RETX_TIMEOUT = 2.0           # Seconds before retransmitting unacknowledged message
MAX_RETX_PER_TICK = 50       # Maximum retransmissions per cycle

# Memory management
HOLD_BACK_LIMIT = 5000       # Maximum messages in holdback buffer
HISTORY_LIMIT = 2000         # Maximum messages in leader's history for retransmit

# Thread-safe printing lock
print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


def get_own_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # no traffic is sent, only routing decision
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def guess_broadcast_ip(ip: str) -> str:
    # best-effort: assume /24 network if typical private ranges
    parts = ip.split(".")
    if len(parts) != 4:
        return "255.255.255.255"
    try:
        a, b, c, d = [int(x) for x in parts]
    except Exception:
        return "255.255.255.255"
    # common home LANs are /24, broadcast ends with .255
    return f"{a}.{b}.{c}.255"


def make_node_id(name: str, ip: str, port: int) -> int:
    # stable 32-bit id
    seed = f"{name}|{ip}|{port}".encode("utf-8")
    h = hashlib.sha256(seed).digest()
    return int.from_bytes(h[:4], "big")


class PeerNode:
    def __init__(self, name: str, broadcast_ip: str):
        self.name = name.strip() or "Peer"
        self.ip = get_own_ip()
        self.port = GROUP_PORT
        self.node_id = make_node_id(self.name, self.ip, self.port)

        self.broadcast_ip = broadcast_ip or "255.255.255.255"

        self.running = True

        # PEER MANAGEMENT STATE
        # peer_id -> dict(name, ip, port, last_seen, remote_ts, delivered)

        self.peers = {}
        self.peers_lock = threading.Lock()

        # LEADER ELECTION STATE
        self.leader_id = None
        self.leader_name = None
        self.leader_addr = None
        self.last_leader_seen = 0.0

        self.election_lock = threading.Lock()
        self.election_in_progress = False

        # RELIABLE ORDERED DELIVERY STATE (FIFO/Total Ordering)
        self.expected_seq = 1           # Next sequence number to deliver
        self.holdback = {}              # seq -> payload tuple (out-of-order buffer)
        self.holdback_lock = threading.Lock()

        # LEADER SEQUENCER STATE (Only used when this node is leader)
        self.global_seq = 0             # Current global sequence counter
        self.global_seq_lock = threading.Lock()

        # Leader reliability tracking:
        # history: seq -> payload tuple (for retransmission)
        self.history = {}
        self.history_lock = threading.Lock()

        # acks: seq -> {peer_id: ack_timestamp} (tracks who acknowledged)
        self.acks = {}
        self.acks_lock = threading.Lock()

        # UDP SOCKET SETUP
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass  # SO_REUSEPORT not available on all platforms

        self.sock.bind(("", self.port))
        self.sock.settimeout(1.0)

    # NETWORKING HELPERS
    
    def send_broadcast(self, payload: str):
        try:
            self.sock.sendto(payload.encode("utf-8"), (self.broadcast_ip, self.port))
        except Exception:
            pass

    def send_unicast(self, payload: str, addr):
        try:
            self.sock.sendto(payload.encode("utf-8"), addr)
        except Exception:
            pass

    # PEER MANAGEMENT & RING TOPOLOGY
    # These functions manage the logical ring used by Chang-Roberts algorithm
    
    def _update_peer(self, peer_id: int, name: str, ip: str, port: int, ts: float, delivered: int):
        now = time.time()
        with self.peers_lock:
            self.peers[peer_id] = {
                "name": name,
                "ip": ip,
                "port": int(port),
                "last_seen": now,
                "remote_ts": ts,
                "delivered": int(delivered),
            }

    def _prune_peers(self):
        now = time.time()
        removed = []
        with self.peers_lock:
            for pid in list(self.peers.keys()):
                if now - self.peers[pid]["last_seen"] > PEER_TIMEOUT:
                    removed.append(pid)
                    del self.peers[pid]
        return removed

    def _ring_order(self):
        with self.peers_lock:
            ids = list(self.peers.keys())
        ids.append(self.node_id)
        return sorted(set(ids))

    def _successor_id(self):
        ring = self._ring_order()
        if len(ring) <= 1:
            return None
        idx = ring.index(self.node_id)
        sid = ring[(idx + 1) % len(ring)]
        return sid if sid != self.node_id else None

    def _successor_addr(self):
        sid = self._successor_id()
        if sid is None:
            return None
        with self.peers_lock:
            info = self.peers.get(sid)
        if not info:
            return None
        return (info["ip"], info["port"])

    def _peer_ids_snapshot(self):
        with self.peers_lock:
            return list(self.peers.keys())

    # CHANG-ROBERTS (LCR) LEADER ELECTION ALGORITHM
    # The Chang-Roberts algorithm elects a leader in a logical ring:
    # 1. Any node can start an election by sending its ID around the ring
    # 2. Each node forwards the maximum ID it has seen
    # 3. When a message returns to the originator with its own ID, it wins
    # 4. The winner announces itself as leader around the ring
    
    def start_election(self, reason=""):
        with self.election_lock:
            self.election_in_progress = True

        succ = self._successor_addr()
        if not succ:
            # No other peers - I become leader by default
            self._become_leader()
            with self.election_lock:
                self.election_in_progress = False
            return

        safe_print(f"\n[SYSTEM] Election start ({reason}) | my_id={self.node_id} -> successor {self._successor_id()}")
        msg = f"ELECTION|{self.node_id}|{self.node_id}"
        self.send_unicast(msg, succ)

        # Failsafe: reset election_in_progress after timeout
        def reset_election():
            time.sleep(10)
            with self.election_lock:
                self.election_in_progress = False
        threading.Thread(target=reset_election, daemon=True).start()

    def handle_election(self, origin_id: int, candidate_id: int):
        # Chang-Roberts:
        # - forward the maximum id
        # - when it returns to origin with same candidate -> candidate is leader
        if origin_id == self.node_id and candidate_id == self.node_id:
            # Message came back to me with my ID - I won!
            self._become_leader()
            with self.election_lock:
                self.election_in_progress = False
            return

        # Compare and possibly replace candidate
        if self.node_id > candidate_id:
            candidate_id = self.node_id

        succ = self._successor_addr()
        if not succ:
            # Ring broken - become leader as fallback
            self._become_leader()
            with self.election_lock:
                self.election_in_progress = False
            return

        # Forward to successor
        self.send_unicast(f"ELECTION|{origin_id}|{candidate_id}", succ)

    def announce_leader(self):
        # send leader message around the ring once
        succ = self._successor_addr()
        if not succ:
            return
        # include current global_seq to stabilize order after leader change
        with self.global_seq_lock:
            gseq = self.global_seq
        msg = f"LEADER|{self.node_id}|{self.node_id}|{self.name}|{self.ip}|{self.port}|{gseq}"
        self.send_unicast(msg, succ)

    def handle_leader(self, origin_id: int, leader_id: int, leader_name: str, leader_ip: str, leader_port: int, leader_seq: int):
        self.leader_id = leader_id
        self.leader_name = leader_name
        self.leader_addr = (leader_ip, int(leader_port))
        self.last_leader_seen = time.time()

        # if leader advertises a seq higher than my expected-1,
        # I can safely fast-forward expected to leader_seq+1 (I might miss history,
        # but delivery order stays correct from now on).
        if leader_seq >= (self.expected_seq - 1):
            self.expected_seq = leader_seq + 1
            with self.holdback_lock:
                # drop anything older to avoid weirdness
                self.holdback = {s: v for s, v in self.holdback.items() if s >= self.expected_seq}

        if leader_id == self.node_id:
            safe_print(f"\n[SYSTEM] ✅ ICH BIN LEADER (id={leader_id})")
        else:
            safe_print(f"\n[SYSTEM] Neuer Leader: {leader_name} (id={leader_id})")

        # Check if message completed the ring
        if origin_id == self.node_id:
            with self.election_lock:
                self.election_in_progress = False
            return

        # Forward to successor
        succ = self._successor_addr()
        if succ:
            fwd = f"LEADER|{origin_id}|{leader_id}|{leader_name}|{leader_ip}|{leader_port}|{leader_seq}"
            self.send_unicast(fwd, succ)

        with self.election_lock:
            self.election_in_progress = False

    def _become_leader(self):
        self.leader_id = self.node_id
        self.leader_name = self.name
        self.leader_addr = (self.ip, self.port)
        self.last_leader_seen = time.time()

        # Stabilize global_seq: pick the maximum delivered from known peers (best effort)
        max_delivered = self.expected_seq - 1
        with self.peers_lock:
            for _, info in self.peers.items():
                try:
                    max_delivered = max(max_delivered, int(info.get("delivered", 0)))
                except Exception:
                    pass

        with self.global_seq_lock:
            self.global_seq = max(self.global_seq, max_delivered)

        safe_print(f"\n[SYSTEM] ✅ Become leader -> id={self.node_id} global_seq={self.global_seq}")
        self.announce_leader()

    # HEARTBEAT MECHANISM (Fault Tolerance)
    # The leader periodically broadcasts heartbeat messages to prove liveness.
    # Followers monitor for heartbeats and trigger election if leader fails.
    
    def leader_heartbeat_loop(self):
        while self.running:
            time.sleep(HB_INTERVAL)
            if self.leader_id == self.node_id:
                with self.global_seq_lock:
                    gseq = self.global_seq
                self.send_broadcast(f"HB|{self.leader_id}|{time.time()}|{gseq}")

    def leader_watchdog_loop(self):
        while self.running:
            time.sleep(1.0)

            # If I'm the leader, no need to watch
            if self.leader_id == self.node_id:
                continue

            ring = self._ring_order()
            
            # Case 1: Peers exist but no leader known
            if len(ring) > 1 and self.leader_id is None:
                self.start_election("no leader yet")
                continue

            # Case 2: Leader timeout - no heartbeat received
            if self.leader_id is not None and (time.time() - self.last_leader_seen > LEADER_TIMEOUT):
                self.leader_id = None
                self.leader_name = None
                self.leader_addr = None
                self.start_election("leader timeout")

    # DYNAMIC DISCOVERY
    # Peers discover each other through periodic HELLO broadcasts.
    # No manual configuration needed - just start the application.
    
    def hello_loop(self):
        while self.running:
            ts = time.time()
            delivered = self.expected_seq - 1
            hello = f"HELLO|{self.node_id}|{self.name}|{self.ip}|{self.port}|{ts}|{delivered}"
            self.send_broadcast(hello)
            time.sleep(HELLO_INTERVAL)

    def peer_prune_loop(self):
        while self.running:
            removed = self._prune_peers()
            if removed:
                # if leader disappeared -> election
                if self.leader_id is not None and self.leader_id in removed:
                    self.leader_id = None
                    self.leader_name = None
                    self.leader_addr = None
                    self.start_election("leader removed")
            time.sleep(1.0)

    # RELIABLE ORDERED MULTICAST (Leader Sequencer Pattern)
    # The leader acts as a sequencer:
    # 1. Peers send chat messages to the leader
    # 2. Leader assigns a global sequence number
    # 3. Leader broadcasts the ordered message
    # 4. Peers deliver in sequence order (holdback for gaps)
    # 5. Peers ACK to leader for reliability
    # 6. Leader retransmits unacknowledged messages
    
    def _leader_next_seq(self) -> int:
        with self.global_seq_lock:
            self.global_seq += 1
            return self.global_seq

    def _leader_store_history(self, seq: int, payload_tuple):
        with self.history_lock:
            self.history[seq] = payload_tuple
            # trim history
            if len(self.history) > HISTORY_LIMIT:
                keys = sorted(self.history.keys())
                for k in keys[: max(0, len(keys) - HISTORY_LIMIT)]:
                    del self.history[k]

    def _ack_init_for_seq(self, seq: int):
        # leader expects ACKs from all current peers (snapshot)
        peer_ids = self._peer_ids_snapshot()
        with self.acks_lock:
            self.acks[seq] = {}
            # leader counts itself as acked
            self.acks[seq][self.node_id] = time.time()
            for pid in peer_ids:
                # not acked yet -> simply absent
                pass

    def _ack_mark(self, seq: int, peer_id: int):
        with self.acks_lock:
            if seq not in self.acks:
                self.acks[seq] = {}
            self.acks[seq][peer_id] = time.time()

    def _ack_all_received(self, seq: int) -> bool:
        with self.acks_lock:
            got = self.acks.get(seq, {})
        # require acks from all currently alive peers + leader itself
        peer_ids = self._peer_ids_snapshot()
        required = set(peer_ids + [self.node_id])
        return required.issubset(set(got.keys()))

    def _deliver_in_order(self):
        # deliver buffered ORDER messages in seq order
        delivered_any = False
        while True:
            with self.holdback_lock:
                tup = self.holdback.get(self.expected_seq)
                if tup is None:
                    break
                del self.holdback[self.expected_seq]

            seq, msg_id, sender_id, sender_name, text = tup
            safe_print(f"\n[{sender_name}] #{seq}: {text}")

            # if I'm not leader, ack to leader
            if self.leader_id is not None and self.leader_id != self.node_id and self.leader_addr:
                self.send_unicast(f"ACK|{self.node_id}|{seq}", self.leader_addr)
            # if I'm leader, mark self ack
            if self.leader_id == self.node_id:
                self._ack_mark(seq, self.node_id)

            self.expected_seq += 1
            delivered_any = True

            # memory safety
            if self.expected_seq > 10**12:
                self.expected_seq = 1

        # holdback safety trim
        if delivered_any:
            with self.holdback_lock:
                if len(self.holdback) > HOLD_BACK_LIMIT:
                    # drop oldest buffered items if someone spams out-of-order
                    keys = sorted(self.holdback.keys())
                    for k in keys[: max(0, len(keys) - HOLD_BACK_LIMIT)]:
                        del self.holdback[k]

    def _buffer_order(self, seq: int, msg_id: str, sender_id: int, sender_name: str, text: str):
        # ignore old
        if seq < self.expected_seq:
            # still ack (helps leader stop retransmitting)
            if self.leader_id is not None and self.leader_id != self.node_id and self.leader_addr:
                self.send_unicast(f"ACK|{self.node_id}|{seq}", self.leader_addr)
            if self.leader_id == self.node_id:
                self._ack_mark(seq, self.node_id)
            return

        with self.holdback_lock:
            if seq not in self.holdback:
                self.holdback[seq] = (seq, msg_id, sender_id, sender_name, text)

        self._deliver_in_order()

    def leader_retransmit_loop(self):
        while self.running:
            time.sleep(RETX_INTERVAL)

            if self.leader_id != self.node_id:
                continue

            # retransmit missing acks
            sent = 0
            now = time.time()

            with self.history_lock:
                hist_items = list(self.history.items())

            # newest first is usually more important
            hist_items.sort(key=lambda x: x[0], reverse=True)

            for seq, tup in hist_items:
                if sent >= MAX_RETX_PER_TICK:
                    break

                # already fully acked? then we can drop ack state eventually
                if self._ack_all_received(seq):
                    continue

                # determine which peers are missing
                peer_ids = self._peer_ids_snapshot()
                required = set(peer_ids + [self.node_id])

                with self.acks_lock:
                    got = dict(self.acks.get(seq, {}))

                missing = list(required.difference(set(got.keys())))
                if not missing:
                    continue

                # resend to missing peers if last ack / attempt is too old
                # (simple: always resend if missing and time passed)
                # NOTE: leader itself is always delivered; missing leader is irrelevant
                for pid in missing:
                    if sent >= MAX_RETX_PER_TICK:
                        break
                    if pid == self.node_id:
                        continue
                    # only resend if peer exists
                    with self.peers_lock:
                        info = self.peers.get(pid)
                    if not info:
                        continue

                    # last time we heard from peer? if peer is too old, skip (prune will remove soon)
                    if now - info["last_seen"] > PEER_TIMEOUT:
                        continue

                    # send ORDER unicast
                    seq2, msg_id, sender_id, sender_name, text = tup
                    payload = f"ORDER|{seq2}|{msg_id}|{sender_id}|{sender_name}|{text}"
                    self.send_unicast(payload, (info["ip"], info["port"]))
                    sent += 1

    # USER INTERFACE
    
    def user_input_loop(self):
        safe_print("----------------------------------------")
        safe_print("PeerTalk++ ready.")
        safe_print("Commands: /peers  /leader  /election  /id  /quit")
        safe_print("Send normal text to chat.")
        safe_print("----------------------------------------")
        while self.running:
            try:
                line = input(f"{self.name}> ").strip()
            except (EOFError, KeyboardInterrupt):
                line = "/quit"

            if not line:
                continue

            if line == "/quit":
                self.running = False
                break

            if line == "/peers":
                with self.peers_lock:
                    peers = dict(self.peers)
                safe_print("\n[PEERS]")
                for pid, info in sorted(peers.items(), key=lambda x: x[0]):
                    age = time.time() - info["last_seen"]
                    safe_print(f"- {info['name']} id={pid} {info['ip']}:{info['port']} (seen {age:.1f}s ago, delivered={info.get('delivered', 0)})")
                safe_print("")
                continue

            if line == "/leader":
                safe_print(f"\n[LEADER] {self.leader_name} id={self.leader_id} addr={self.leader_addr}\n")
                continue

            if line == "/election":
                self.start_election("manual")
                continue

            if line == "/id":
                safe_print(f"\n[ME] name={self.name} id={self.node_id} ip={self.ip}:{self.port} bcast={self.broadcast_ip}\n")
                continue

            # normal chat message:
            msg_id = str(uuid.uuid4())

            # If leader is me -> I sequence it
            if self.leader_id == self.node_id:
                self._leader_accept_chat(msg_id, self.node_id, self.name, line)
                continue

            # If I have a leader -> send unicast to leader (reliable sequencing)
            if self.leader_addr and self.leader_id is not None:
                payload = f"CHATU|{msg_id}|{self.node_id}|{self.name}|{line}"
                self.send_unicast(payload, self.leader_addr)
            else:
                # no leader known: trigger election and still broadcast as best-effort
                self.start_election("send without leader")
                payload = f"CHATU|{msg_id}|{self.node_id}|{self.name}|{line}"
                self.send_broadcast(payload)

    # LEADER MESSAGE PROCESSING
    
    def _leader_accept_chat(self, msg_id: str, sender_id: int, sender_name: str, text: str):
        # leader assigns global sequence and multicasts ORDER
        seq = self._leader_next_seq()
        tup = (seq, msg_id, sender_id, sender_name, text)

        self._leader_store_history(seq, tup)
        self._ack_init_for_seq(seq)

        payload = f"ORDER|{seq}|{msg_id}|{sender_id}|{sender_name}|{text}"
        self.send_broadcast(payload)

        # leader also delivers locally through the same path
        self._buffer_order(seq, msg_id, sender_id, sender_name, text)

    # NETWORK MESSAGE RECEIVER
    
    def receiver_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                continue

            try:
                raw = data.decode("utf-8", errors="replace")
            except Exception:
                continue

            parts = raw.split("|")
            if not parts:
                continue

            mtype = parts[0]

            # HELLO|id|name|ip|port|ts|delivered - Dynamic Discovery
            if mtype == "HELLO" and len(parts) >= 7:
                try:
                    pid = int(parts[1])
                    name = parts[2]
                    ip = parts[3]
                    port = int(parts[4])
                    ts = float(parts[5])
                    delivered = int(parts[6])
                except Exception:
                    continue

                if pid == self.node_id:
                    continue

                self._update_peer(pid, name, ip, port, ts, delivered)

                # if we don't have a leader yet, start election when ring exists
                if self.leader_id is None and len(self._ring_order()) > 1:
                    self.start_election("peers discovered")
                continue

            # HB|leader_id|ts|global_seq - Leader Heartbeat
            if mtype == "HB" and len(parts) >= 4:
                try:
                    leader_id = int(parts[1])
                    gseq = int(parts[3])
                except Exception:
                    continue
                self.leader_id = leader_id
                self.last_leader_seen = time.time()
                # learn order baseline
                if gseq >= (self.expected_seq - 1):
                    self.expected_seq = max(self.expected_seq, gseq + 1)
                continue

            # CHATU|msg_id|sender_id|name|text - Chat request to leader
            if mtype == "CHATU" and len(parts) >= 5:
                msg_id = parts[1]
                try:
                    sender_id = int(parts[2])
                except Exception:
                    sender_id = -1
                sender_name = parts[3]
                text = "|".join(parts[4:])

                # only leader sequences chat; non-leaders ignore
                if self.leader_id == self.node_id:
                    self._leader_accept_chat(msg_id, sender_id, sender_name, text)
                continue

            # ORDER|seq|msg_id|sender_id|name|text - Ordered message from leader
            if mtype == "ORDER" and len(parts) >= 6:
                try:
                    seq = int(parts[1])
                    msg_id = parts[2]
                    sender_id = int(parts[3])
                    sender_name = parts[4]
                    text = "|".join(parts[5:])
                except Exception:
                    continue

                self._buffer_order(seq, msg_id, sender_id, sender_name, text)
                continue

            # ACK|peer_id|seq - Delivery acknowledgment to leader
            if mtype == "ACK" and len(parts) >= 3:
                if self.leader_id != self.node_id:
                    continue
                try:
                    peer_id = int(parts[1])
                    seq = int(parts[2])
                except Exception:
                    continue
                self._ack_mark(seq, peer_id)
                continue

            # ELECTION|origin_id|candidate_id - Chang-Roberts election
            if mtype == "ELECTION" and len(parts) >= 3:
                try:
                    origin_id = int(parts[1])
                    candidate_id = int(parts[2])
                except Exception:
                    continue
                self.handle_election(origin_id, candidate_id)
                continue

            # LEADER|origin|leader_id|leader_name|leader_ip|leader_port|leader_seq
            if mtype == "LEADER" and len(parts) >= 7:
                try:
                    origin_id = int(parts[1])
                    leader_id = int(parts[2])
                    leader_name = parts[3]
                    leader_ip = parts[4]
                    leader_port = int(parts[5])
                    leader_seq = int(parts[6])
                except Exception:
                    # keep backward compatibility if older format
                    try:
                        origin_id = int(parts[1])
                        leader_id = int(parts[2])
                        leader_name = parts[3]
                        leader_ip = parts[4]
                        leader_port = int(parts[5])
                        leader_seq = 0
                    except Exception:
                        continue

                self.handle_leader(origin_id, leader_id, leader_name, leader_ip, leader_port, leader_seq)
                continue

    # APPLICATION LIFECYCLE
    
    def start(self):
        safe_print("========================================")
        safe_print(" PeerTalk++ UDP | Discovery + LCR + Reliable Ordered Multicast ")
        safe_print("========================================")
        safe_print(f"ME: {self.name} | id={self.node_id} | ip={self.ip}:{self.port}")
        safe_print(f"Broadcast: {self.broadcast_ip}:{self.port}")
        safe_print("Same LAN required. Allow UDP port in firewall.")
        safe_print("----------------------------------------\n")

        threads = [
            threading.Thread(target=self.receiver_loop, daemon=True),
            threading.Thread(target=self.hello_loop, daemon=True),
            threading.Thread(target=self.peer_prune_loop, daemon=True),
            threading.Thread(target=self.leader_heartbeat_loop, daemon=True),
            threading.Thread(target=self.leader_watchdog_loop, daemon=True),
            threading.Thread(target=self.leader_retransmit_loop, daemon=True),
        ]
        for t in threads:
            t.start()

        # initial election if peers already present
        time.sleep(1.0)
        if len(self._ring_order()) > 1:
            self.start_election("startup")

        self.user_input_loop()

        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass
        safe_print("\n[SYSTEM] bye 👋")

# MAIN ENTRY POINT

def main():
    parser = argparse.ArgumentParser(description="PeerTalk++ UDP Broadcast Chat (Reliable Ordered Multicast + LCR)")
    parser.add_argument("--bcast", type=str, default="", help="Broadcast IP (e.g. 192.168.178.255). If empty -> auto guess.")
    parser.add_argument("--name", type=str, default="", help="Your display name")
    args = parser.parse_args()

    name = args.name.strip()
    if not name:
        try:
            name = input("Dein Name: ").strip()
        except Exception:
            name = "Peer"

    ip = get_own_ip()
    bcast = args.bcast.strip() if args.bcast.strip() else guess_broadcast_ip(ip)

    node = PeerNode(name, bcast)
    node.start()


if __name__ == "__main__":
    main()