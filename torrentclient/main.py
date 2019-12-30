import os
import logging
import argparse
from typing import List
from multiprocessing import Queue, Process, Value, Lock

from torrentclient.mytorrent import MyTorrent
from torrentclient.peerinteract.peer import Peer
from torrentclient.peerinteract.connection import PeerConnection
from torrentclient.peerinteract.getpiece import GetPiece


QUEUE_STOP_FLAG = None


PARALLEL_TRACKERS_COUNT = 40
"""number of parallel processes establishing a connection with a tracker"""


def peers_from_tracker(tracker_url: str, torrent: MyTorrent) -> List[Peer]:
    """returns list of peers of a torrent from a specific tracker"""
    from torrentclient.trackerinteract.tracker import Tracker
    from torrentclient.trackerinteract.requestpeers import RequestPeers
    from torrentclient.trackerinteract.handleresponse import HandleResponse

    try:
        response = RequestPeers(Tracker(tracker_url), torrent).send()
        peers = HandleResponse(response).get_peers()
    except (Tracker.Exception, RequestPeers.Exception, HandleResponse.Exception) as e:
        RequestPeers.logger.warning("Failed getting peers of '{}' from {}: {}".format(
            torrent.name, tracker_url, e))
        return []
    else:
        return peers


def peers_from_trackers(torrent: MyTorrent) -> List[Peer]:
    """returns a list of peers from all trackers from torrent file and local cache"""
    from torrentclient.trackerinteract.parallel import map_parallel
    if torrent.trackers is not None:
        trackers = torrent.trackers
    else:
        trackers = []
    trackers.extend(
        [tracker[:-1]] for tracker in open(os.path.join(os.getcwd(), "tests\\trackers.txt"), "r").readlines())
    map_args = [(tracker_url[0], torrent) for tracker_url in trackers]
    peers = map_parallel(peers_from_tracker, map_args, PARALLEL_TRACKERS_COUNT)
    return set(peers)  # remove duplicates


def next_connected_peer(peers: Queue, torrent: MyTorrent) -> PeerConnection:
    """returns a PeerConnection from the peers queue"""
    from torrentclient.peerinteract.handshake import PeerHandshake
    while not peers.empty():
        peer = peers.get()
        # TODO: use priority queue
        #  so that recently connected/working peers will be tried again earlier than other peers
        #  but also do not put peers until finished using them
        peers.put(peer)  # move back to end of queue
        hs = PeerHandshake(peer=peer, torrent=torrent)
        try:
            connection = hs.handshake()
        except PeerHandshake.Exception as e:
            hs.logger.error(e)
        else:
            hs.logger.info("Connected to {}!".format(peer))
            return connection


def write_piece(pieces_queue: Queue, peers_queue: Queue, torrent: MyTorrent, pieces_counter: Value, counter_lock: Lock):
    """pops a piece index and a PeerConnection from the corresponding queues,
    tries to get and write the piece index with the PeerConnection.
    in case of failure, puts piece index back to queue."""
    piece_idx = pieces_queue.get()
    connection = next_connected_peer(peers_queue, torrent)
    while connection is not None and piece_idx is not QUEUE_STOP_FLAG:
        GetPiece.logger.debug("Process {}: piece_idx={}, pid={}".format(connection.peer, piece_idx, os.getpid()))
        try:
            piece = GetPiece(peer_connection=connection, torrent=torrent, piece_idx=piece_idx).get()
        except Exception as e:
            GetPiece.logger.error("Failed to get piece #{} with {}: {}".format(piece_idx, connection, e))
            connection.socket.close()
            connection = next_connected_peer(peers_queue, torrent)
        else:
            GetPiece.logger.info("Successfully obtained piece #{} with {}".format(piece_idx, connection))
            with open(torrent.out_filename, "wb+") as out:
                out.seek(piece_idx*torrent.my_piece_size)
                out.write(piece)
            with counter_lock:
                pieces_counter.value += 1
            piece_idx = pieces_queue.get()
    if connection is not None:
        connection.socket.close()


def feed_pieces_queue(pieces_queue: Queue, pieces_count: int, parallel_peers_count: int):
    """puts piece indices and stop flags to `pieces_queue`"""
    for idx in range(pieces_count):
        pieces_queue.put(idx)
    for p_count in range(parallel_peers_count):
        pieces_queue.put(QUEUE_STOP_FLAG)
    GetPiece.logger.debug("Done feeding queue: {}".format(pieces_queue.qsize()))


def update_progress(total_pieces: int, pieces_counter: Value, counter_lock: Lock):
    """updates file with number of pieces obtained out of the total amount"""
    import time
    while True:
        with open("progress.txt", "w+") as out:
            with counter_lock:
                out.write("{}/{} downloaded".format(
                    str(pieces_counter.value), total_pieces))
        time.sleep(1)
        with counter_lock:
            if pieces_counter.value < total_pieces:
                break


def write_pieces(peers_queue: Queue, torrent: MyTorrent):
    """tries getting all pieces in parallel using peers queue and piece indices queue"""
    GetPiece.logger.info("Trying to get {} pieces".format(torrent.piece_count))
    pieces_queue = Queue()
    processes = []
    parallel_peers_count = peers_queue.qsize() // 5
    pieces_counter = Value('i', 0)
    counter_lock = Lock()
    GetPiece.logger.debug("parallel_peers_count={}".format(parallel_peers_count))

    # run `parallel_peers_count` identical processes of trying to write a piece
    for _ in range(parallel_peers_count):
        peer_process = Process(target=write_piece, args=(pieces_queue, peers_queue, torrent, pieces_counter, counter_lock))
        processes.append(peer_process)

    # helper process to update progress of download
    progress_process = Process(target=update_progress, args=(torrent.piece_count, pieces_counter, counter_lock))
    processes.append(progress_process)

    for process in processes:
        process.start()
    feed_pieces_queue(pieces_queue, torrent.piece_count, parallel_peers_count)

    for process in processes[:-1]:  # except last daemon process
        process.join()


def partition_content_to_files(torrent: MyTorrent):
    """partition the entire torrent content to actual files using the torrent meta-info"""
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    with open(torrent.out_filename, "rb") as content:
        for path, length in torrent.paths_and_lengths:
            with open(os.path.join("downloads", path), "wb+") as out:
                out.write(content.read(length))
                print("Written {}!".format(path))


def get_files(torrent_path: str):
    torrent = MyTorrent.read(filepath=torrent_path)
    peers_queue = Queue()
    for peer in peers_from_trackers(torrent):
        peers_queue.put(peer)
    write_pieces(peers_queue, torrent)
    partition_content_to_files(torrent)
    print("Done downloading torrent content!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog='torrent-client')
    parser.add_argument("path")
    parser.add_argument('-v', "--verbose", action="store_true")
    args = parser.parse_args()
    log_level = logging.INFO
    if args.verbose:
        log_level = logging.DEBUG
    get_files(torrent_path=args.path)
