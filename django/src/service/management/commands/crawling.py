import binascii
import json
import os
import requests
from datetime import datetime
from django.core.management.base import BaseCommand
from django.db import transaction
from pytz import timezone, utc
from service.models import (
  ParkingLotModel,
  TimePriceTableModel,
)


class Command(BaseCommand):
  help = 'Crawling informations of Seoul public parking lots'

  def handle(self, *args, **options):
    key = os.environ['OPEN_API_KEY']

    steps = 100  # 1000
    begin = 1
    end   = steps

    KST = timezone('Asia/Seoul')
    now = datetime.utcnow()
    ver = str(KST.localize(now))

    loop = True
    while loop:
      print(f'BEGIN({begin}) - END({end})')

      url = f'http://openapi.seoul.go.kr:8088/{key}/json/GetParkInfo/{begin}/{end}/'
      res = requests.get(url)
      obj = res.json()

      r = obj['GetParkInfo']['RESULT']
      _assert_result(r)

      total = obj['GetParkInfo']['list_total_count']
      row   = obj['GetParkInfo']['row']
      end   = begin + len(row) - 1

      _update_parking_lots(row, now, ver)

      break  # temp for test

      loop  = (end < total)
      begin = end + 1
      end   = min(total, end + steps)

    n = ParkingLotModel.objects.exclude(version=ver).delete()
    n = n[1].get('service.ParkingLotModel', 0)
    print(f'ParkingLotModel: {n} rows are deleted')


def _assert_result(result: dict) -> None:
  c = result['CODE']
  m = result['MESSAGE']
  assert c == 'INFO-000', f'[CODE: {c}] {m}'


@transaction.atomic
def _update_parking_lots(parking_lots: list, now: datetime, version: str) -> None:
  code_list = {item['PARKING_CODE'] for item in parking_lots}
  code_list = list(code_list)

  rows = ParkingLotModel.objects.filter(code__in=code_list)
  rows = {row.code:row for row in rows}

  weekday = now.weekday()
  holiday = False  #48 공공데이터포털의 API가 동작하지 않아 휴일을 구분할 수 없다.

  for item in parking_lots:
    code = int(item['PARKING_CODE'])
    json_string = json.dumps(item, ensure_ascii=False)
    crc32 = binascii.crc32(json_string.encode('utf8'))

    try:
      row = rows[code]
      update = (row.crc32 != crc32)
      update_fields = ['version']
    except KeyError:
      row = ParkingLotModel(code=code)
      update = True
      update_fields = None

    if update:
      row.name = item['PARKING_NAME']
      row.address = item['ADDR']
      row.phone_num = _regulate_phone_number(item['TEL'])
      row.lat = item['LAT']
      row.lng = item['LNG']
      row.json_string = json_string
      row.crc32 = crc32
      if update_fields != None:
        update_fields += ['name', 'address', 'phone_num', 'json_string', 'crc32']
    row.version = version
    row.save(update_fields=update_fields)

    prices = _calc_time_pricing_table(code, item, weekday, holiday)
    for time, price in enumerate(prices, -1):
      # -1: 월정액
      #  0: 일 최대요금
      #. 1: 1시간 요금
      #. 2: 2시간 요금
      #. 3: 3시간 요금
      #. 4: 4시간 요금
      TimePriceTableModel.objects.update_or_create(
        parking_lot=row,
        time=time,
        defaults= {
          'price': price,
        }
      )


def _regulate_phone_number(tel: str) -> str:
  assert isinstance(tel, str)
  tel = tel.strip().replace(')', '-')
  if not '~' in tel:
    return tel
  p = tel[:-3]
  b = int(tel[-3:-2])
  e = int(tel[-1:])
  tel = {f'{p}{n}' for n in range(b, e+1)}
  return ','.join(tel)


def _calc_time_pricing_table(code: int, item: dict, weekday: int, holiday: bool) -> list:
  retval = [_price_per_month(item)]

  # 무료 / 토요일 무료 / 일요일 무료
  isfree = (item['PAY_YN'] == 'N')
  isfree = isfree or (weekday == 5 and item['SATURDAY_PAY_YN'] == 'N')
  isfree = isfree or (weekday == 6 and item['HOLIDAY_PAY_YN'] == 'N')
  if isfree:
    for _ in range(0, 5):
      retval.append(0)
  else:
    retval.append(_price_per_day(item, weekday, holiday))
    for hour in range(1, 5):
      retval.append(_price_per_hour(item, hour))
  return retval


def _price_per_month(item: dict) -> int:
  v = item['FULLTIME_MONTHLY']
  return (_price_per_day(item, 0, False) * 30 if v == '' or int(v) == 0 else int(v))


def _price_per_day(item: dict, weekday: int, holiday: bool) -> int:
  v = int(item['DAY_MAXIMUM'])
  if v > 0:
    return v

  def to_minute(v: str) -> int:
    h = int(int(v) / 100)
    m = int(v) % 100
    return h * 60 + m

  if weekday in [5, 6]:
    begin = to_minute(item['WEEKEND_BEGIN_TIME'])
    end   = to_minute(item['WEEKEND_END_TIME'])
  elif holiday:
    begin = to_minute(item['HOLIDAY_BEGIN_TIME'])
    end   = to_minute(item['HOLIDAY_END_TIME'])
  else:
    begin = to_minute(item['WEEKDAY_BEGIN_TIME'])
    end   = to_minute(item['WEEKDAY_END_TIME'])

  return _price_per_minute(item, (0 if begin < end else 24*60) - begin + end)


def _price_per_hour(item: dict, hours: int) -> int:
  return _price_per_minute(item, hours * 60)


def _price_per_minute(item: dict, minutes: int) -> int:
  base_price = int(item['RATES'])
  base_time_rate = int(item['TIME_RATE'])
  extra_price = int(item['ADD_RATES'])
  extra_time_rate = int(item['ADD_TIME_RATE'])

  price = base_price
  if extra_price > 0:
    extra_time = minutes - base_time_rate
    price += int(extra_time / extra_time_rate) * extra_price
  return price
