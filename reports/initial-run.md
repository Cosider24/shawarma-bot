# Фактическая приёмка

UTC: 2026-10-29T17:59:49.854363+00:00

Команда: `python -m tests.acceptance --rounds 5 --buyers 20 --stock 1`

Проверок: 16; успешно: 14.

Изолированные временные SQLite, два настоящих HTTP-сервера, отдельные соединения, подписанные учебные личности. Сброс — удаление только созданных временных каталогов.

## 1. Цепочка остатков

Ожидалось: [[10, 0, 10], [10, 3, 7], [7, 0, 7], [7, 2, 5], [7, 0, 7], [10, 0, 10]]

Получено: [[10, 0, 10], [10, 3, 7], [7, 0, 7], [7, 2, 5], [7, 0, 7], [10, 0, 10]]

## 2. Одна единица / 20 покупателей

Ожидалось: {"success": 1, "shortage": 19, "rounds": 5}

Получено: [{"round": 1, "success": 1, "shortage": 19}, {"round": 2, "success": 1, "shortage": 19}, {"round": 3, "success": 1, "shortage": 19}, {"round": 4, "success": 1, "shortage": 19}, {"round": 5, "success": 1, "shortage": 19}]

## 3. Десять единиц / 50 покупателей

Ожидалось: {"success": 10, "shortage": 40, "rounds": 5}

Получено: [{"round": 1, "success": 10, "shortage": 40}, {"round": 2, "success": 10, "shortage": 40}, {"round": 3, "success": 10, "shortage": 40}, {"round": 4, "success": 10, "shortage": 40}, {"round": 5, "success": 10, "shortage": 40}]

## Параметризуемая конкуренция

Ожидалось: {"success": 1, "shortage": 19, "rounds": 5}

Получено: [{"round": 1, "success": 1, "shortage": 19}, {"round": 2, "success": 1, "shortage": 19}, {"round": 3, "success": 1, "shortage": 19}, {"round": 4, "success": 1, "shortage": 19}, {"round": 5, "success": 1, "shortage": 19}]

## 4. Откат всего заказа

Ожидалось: "409, A=3/0/3, заказов 0"

Получено: {"error": "Сейчас нет в наличии нужного количества. Обновите корзину."}

## 5. Повторы и другой состав

Ожидалось: "1 заказ; конфликт 409; смена цены не меняет повтор"

Получено: [[200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": false}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}], [200, {"id": 1, "duplicate": true}]]

## 6. Пять оплат и поздний canceled

Ожидалось: "1 продажа, 120000 коп."

Получено: {"from": "2000-01-01", "to": "2099-12-31", "receipts": 120000, "receipts_count": 1, "refunds": 0, "refunds_count": 0, "expenses": 0, "expenses_count": 0, "cogs_paid": 70000, "cogs_refunded": 0, "cash_flow": 120000, "result": 50000, "restored_cost_orders": 0}

## 7. Повтор отмены и истечение

Ожидалось: "2 освобождения, поступлений денег нет"

Получено: [2, 0, 2]

## 8. Гонки и точная граница

Ожидалось: "До срока продажа, на/после — возврат; один победитель"

Получено: [{"now": 2000000119.999, "sale_accepted": 1}, {"now": 2000000120, "sale_accepted": 0}, {"now": 2000000120.001, "sale_accepted": 0}, {"round": 1, "winner": "payment → consumed", "server_time": 2000000000, "expires_at": 2000000120}, {"round": 2, "winner": "cancel → released → payment_conflict → refund", "server_time": 2000000000, "expires_at": 2000000120}, {"round": 3, "winner": "cancel → released → payment_conflict → refund", "server_time": 2000000000, "expires_at": 2000000120}, {"round": 4, "winner": "cancel → released → payment_conflict → refund", "server_time": 2000000000, "expires_at": 2000000120}, {"round": 5, "winner": "cancel → released → payment_conflict → refund", "server_time": 2000000000, "expires_at": 2000000120}]

## 9. Поздняя оплата A после продажи Б

Ожидалось: "Поток 1200 ₽, себестоимость 700 ₽, результат 500 ₽"

Получено: {"from": "2000-01-01", "to": "2099-12-31", "receipts": 240000, "receipts_count": 2, "refunds": 120000, "refunds_count": 1, "expenses": 0, "expenses_count": 0, "cogs_paid": 70000, "cogs_refunded": 0, "cash_flow": 120000, "result": 50000, "restored_cost_orders": 0}

## 10. Перезапуск с просроченным резервом

Ожидалось: "1 освобождение до двух интервалов"

Получено: [1, 0, 1]

## 12. Остановка до commit

Ожидалось: "Откат и успешный повтор, 1 заказ"

Получено: [1, 1, 0]

## 13. Покупатель, чужой резерв, цена, срок

Ожидалось: "403 / 404 / 400 / 401, данные неизменны"

Получено: "Соответствует"

## Дополнительно: рецепты и снимки

Ожидалось: "2470 ₽ / себестоимость 1428 ₽ / общий резерв 2"

Получено: [{"id": 1, "order_id": 1, "product_name": "Шаурма классическая (+ Двойной сыр; + Халапеньо; без лука)", "price": 1270, "qty": 1, "cost_kop": 72800, "cost_source": "order", "product_id": 1, "options_json": "{\"add\": [\"cheese\", \"jalapeno\"], \"remove\": [\"Лук\"]}"}, {"id": 2, "order_id": 1, "product_name": "Шаурма классическая", "price": 1200, "qty": 1, "cost_kop": 70000, "cost_source": "order", "product_id": 1, "options_json": "{\"add\": [], \"remove\": []}"}]

## Провалы: test_11_restart_refund_error_lost_response (tests.test_acceptance.Acceptance.test_11_restart_refund_error_lost_response)

```text
Traceback (most recent call last):
  File "C:\Users\torho\Documents\Codex\2026-10-29\mini-app-x20-2\outputs\shawarma_bot\tests\test_acceptance.py", line 271, in test_11_restart_refund_error_lost_response
  File "C:\Users\torho\Documents\Codex\2026-10-29\mini-app-x20-2\outputs\shawarma_bot\tests\test_acceptance.py", line 148, in eventually
    got=fn()
AssertionError: 'refund_pending' != 'refunded'
- refund_pending
?       -- - ---
+ refunded


```

## Провалы: test_15_confirmed_refund_only (tests.test_acceptance.Acceptance.test_15_confirmed_refund_only)

```text
Traceback (most recent call last):
  File "C:\Users\torho\Documents\Codex\2026-10-29\mini-app-x20-2\outputs\shawarma_bot\tests\test_acceptance.py", line 319, in test_15_confirmed_refund_only
    def test_14_recipe_snapshots(self):
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\torho\Documents\Codex\2026-10-29\mini-app-x20-2\outputs\shawarma_bot\tests\test_acceptance.py", line 148, in eventually
    got=fn()
AssertionError: 'refund_pending' != 'refunded'
- refund_pending
?       -- - ---
+ refunded


```