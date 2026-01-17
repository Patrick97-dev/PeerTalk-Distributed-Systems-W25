# PeerTalk

P2P Chat Application for Distributed Systems Course

**Group 20**

---

## What is this?

A peer-to-peer chat app where all nodes are equal. No central server - peers find each other automatically and elect a leader who coordinates message ordering.

Built with Python using UDP broadcast.

## How to run

```bash
python3 peertalk.py
```

Or with arguments:
```bash
python3 peertalk.py --name "Alice" --bcast "192.168.1.255"
```

You need at least 2 peers in the same LAN to see it working. Make sure UDP port 50000 is open in your firewall.

## Commands

| Command | What it does |
|---------|--------------|
| `/peers` | Shows all connected peers |
| `/leader` | Shows current leader |
| `/election` | Manually triggers a new election |
| `/id` | Shows your own node info |
| `/quit` | Exit |

Anything else you type gets sent as a chat message.

## Implemented Concepts

### 1. Dynamic Discovery
Peers broadcast HELLO messages every 2 seconds. When a new peer joins the network, others discover it automatically - no config needed.

If a peer doesn't send HELLO for 6 seconds, it's considered offline and gets removed.

### 2. Leader Election (Chang-Roberts)
We use the LCR ring algorithm:
- All peers form a logical ring sorted by their node ID
- When election starts, a message travels around the ring
- Each node compares IDs and forwards the highest one
- When the message comes back to the starter with the same ID, that node wins
- Winner announces itself as leader to everyone

Election triggers:
- On startup when peers are found
- When leader times out (no heartbeat for 6s)
- When leader disconnects
- Manual with `/election`

### 3. Reliable Ordered Multicast
The leader acts as a sequencer:
1. You send your message to the leader
2. Leader assigns a global sequence number
3. Leader broadcasts the ORDER message
4. Everyone delivers messages in sequence order
5. Peers send ACK back to leader
6. Leader retransmits if ACK is missing

This guarantees total ordering - everyone sees messages in the same order.

### 4. Fault Tolerance
- **Heartbeat**: Leader broadcasts "I'm alive" every 2 seconds
- **Timeout detection**: No heartbeat for 6s = leader is dead = new election
- **Peer pruning**: Inactive peers get removed automatically
- **Holdback buffer**: Out-of-order messages wait until gaps are filled

## Message Protocol

| Message | Format | Description |
|---------|--------|-------------|
| HELLO | `HELLO\|id\|name\|ip\|port\|timestamp\|delivered_seq` | Discovery |
| HB | `HB\|leader_id\|timestamp\|global_seq` | Leader heartbeat |
| CHATU | `CHATU\|msg_id\|sender_id\|name\|text` | Chat request to leader |
| ORDER | `ORDER\|seq\|msg_id\|sender_id\|name\|text` | Ordered message from leader |
| ACK | `ACK\|peer_id\|seq` | Delivery confirmation |
| ELECTION | `ELECTION\|origin_id\|candidate_id` | Election message |
| LEADER | `LEADER\|origin\|leader_id\|name\|ip\|port\|seq` | Leader announcement |

## Requirements

- Python 3.x
- All peers in same LAN/WLAN
- UDP port 50000 open
- Broadcast enabled on network

## Known Limitations

- UDP can lose packets (we handle this with ACKs + retransmit, but it's not 100% bulletproof)
- If network is partitioned, you might get multiple leaders (split brain)
- History buffer is limited to 2000 messages

## Project Structure

```
peertalk.py    - Main application (single file)
README.md      - This file
```

We kept it as a single file to make it easy to run and demo.