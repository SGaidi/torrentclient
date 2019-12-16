import os
import queue

from torrentclient.torcode.mytorrent import MyTorrent
from torrentclient.client.trackerinteract.parallel import map_parallel
from torrentclient.client.trackerinteract.tracker import Tracker
from torrentclient.client.trackerinteract.requestpeers import RequestPeers
from torrentclient.client.trackerinteract.handleresponse import HandleResponse
from torrentclient.client.peerinteract.handshake import PeerHandshake
from torrentclient.client.peerinteract.connection import PeerConnection
from torrentclient.client.peerinteract.getpiece import GetPiece


def add_peers(tracker_url: str, torrent: MyTorrent) -> []:
    try:
        response = RequestPeers(Tracker(tracker_url), torrent).send()
    except (Tracker.Exception, RequestPeers.Exception) as e:
        RequestPeers.logger.warning("Failed requesting peers of '{}' from {}: {}".format(
            torrent.name, tracker_url, e))
        return []

    hr = HandleResponse(response)
    peers = []
    try:
        peers = hr.get_peers()
    except HandleResponse.Exception as e:
        HandleResponse.logger.warning("Failed to get peers with {}: {}".format(hr, e))
    if len(peers) == 0:
        HandleResponse.logger.warning("Could not get any peers with {}".format(hr))
    return peers


def next_connected_peer(peers: queue.Queue, torrent: MyTorrent) -> PeerConnection:
    while not peers.empty():
        peer = peers.get()
        if (peer.ip_address, peer.port) in next_connected_peer.seen_peers:
            continue
        else:
            next_connected_peer.seen_peers.add((peer.ip_address, peer.port))
        hs = PeerHandshake(peer=peer, torrent=torrent)
        try:
            connection = hs.handshake()
        except PeerHandshake.Exception as e:
            hs.logger.error("Failed to handshake {}: {}".format(peer, e))
        else:
            hs.logger.info("Connected to {}!".format(peer))
            return connection
    return None
next_connected_peer.seen_peers = set()


def get_content(torrent_path: str):
    torrent = MyTorrent.read(filepath=torrent_path)

    """
    if torrent.trackers is not None:
        trackers = torrent.trackers
    else:
        trackers = []
    trackers.extend([tracker[:-1]] for tracker in open(os.path.join(os.getcwd(), "tests\\trackers.txt"), "r").readlines())
    peers = map_parallel(add_peers, [(tracker_url[0], torrent) for tracker_url in trackers], 30)
    """

    # TODO: remove:
    from torrentclient.client.peerinteract.peer import Peer
    peers = [Peer(*(line.replace('\n', '').split(':'))) for line in open("ubuntu18_peers.txt", 'r').readlines()]

    peers_queue = queue.Queue()

    # TODO: remove:
    #with open("ubuntu18_peers.txt", "w+") as out:
    #   out.write("\n".join("{}:{}".format(peer.ip_address, peer.port) for peer in peers))

    for peer in peers:
        peers_queue.put(peer)

    connection = next_connected_peer(peers_queue, torrent)
    pieces = []
    piece_idx = 0
    while piece_idx < torrent.pieces and connection is not None:
        try:
            piece = GetPiece(peer_connection=connection, torrent=torrent, piece_idx=piece_idx).get()
        except (GetPiece.Exception, PeerConnection.Exception) as e:
            GetPiece.logger.error("Failed to get piece #{} with {}: {}".format(piece_idx, connection, e))
            connection.socket.close()
            connection = next_connected_peer(peers_queue, torrent)
        else:
            pieces.append(piece)
            piece_idx += 1
    if connection is not None:
        connection.socket.close()
    if len(pieces) != torrent.pieces:
        raise RuntimeError("Could not get all pieces of {}! Got only {}".format(torrent.name, len(pieces)))
