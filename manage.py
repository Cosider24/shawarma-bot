"""Явные команды обслуживания. Никогда не сбрасывает существующую базу."""
import argparse
import json
import os
import uuid
from pathlib import Path

import db
import stock


def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('init');sub.add_parser('stock');sub.add_parser('seed-demo')
    args=p.parse_args()
    db.init_db()
    if args.command=='seed-demo':
        for item in stock.catalog():
            stock.receive(item['id'],10,'demo-init',f'initial-stock-{item["id"]}')
        print('Поступления по 10 шт. проведены (повтор безопасен).')
    elif args.command=='stock':
        print(json.dumps(stock.catalog(),ensure_ascii=False,indent=2))
    else:
        print(f'Схема готова: {Path(db.DB_PATH).resolve()}. Остатки не добавлялись.')


if __name__=='__main__':main()
