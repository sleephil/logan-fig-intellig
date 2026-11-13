"""Daily incremental import; --bulk explicitly loads official historical ZIPs first."""
import argparse
import logging
from app import sync
if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--bulk',action='store_true');args=p.parse_args()
    logging.basicConfig(level=logging.INFO)
    result=sync(bulk=args.bulk,trigger='bulk-backfill' if args.bulk else 'scheduled')
    raise SystemExit(0 if result in ('success','already_running') else 1)
