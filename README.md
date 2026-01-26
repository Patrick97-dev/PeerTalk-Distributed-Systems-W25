# PeerTalk

P2P Chat Application for Distributed Systems Course

**Group 20**

---

## What is this?

A peer-to-peer chat app where all nodes are equal. No central server - peers find each other automatically and elect a leader who coordinates message ordering.

Built with Python using UDP broadcast.

## How to run

```bash
python3 main.py
```

Or with arguments:
```bash
python3 main.py --name "Your Name" --bcast "192.168.1.255"
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

We kept it as a single file to make it easy to run and demo.
