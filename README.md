# Магазин звёзд и Premium через Fragment

Магазин продаёт Telegram Stars и Telegram Premium. Покупка для покупателя идёт на [Fragment API](https://www.fragmentapi.com/docs): звёзды и Premium отправляются на Telegram username. Своя цена считается сверху себестоимости Fragment.

Fragment API уже включает свою комиссию 10% в сумму заказа и в пакеты Premium. Наценка магазина к этой комиссии не прибавляет те же 10% ещё раз.

```text
цена покупателя = себестоимость Fragment × (1 + SHOP_MARKUP_PERCENT / 100)
```

По умолчанию `SHOP_MODE=dry-run`: Fragment не вызывается, в заказ пишется та же покупка, которую отправил бы живой режим. `SHOP_MARKUP_PERCENT` по умолчанию `0`.

## Что нужно снаружи

1. Ключ Fragment API в `API_TOKEN` (или `FRAGMENT_API_KEY`). Это заголовок `API-Key`. В репозиторий его класть нельзя.
2. Баланс TON или USDT на TON в кабинете Fragment API. Валюта заказа — `SHOP_WALLET` (`USDT` по умолчанию).
3. Для звёзд — `SHOP_STAR_UNIT_COST`: себестоимость одной звезды в валюте кошелька. У Fragment нет прайса на звёзды до создания заказа, поэтому цена витрины считается из этой ставки. Берите её как `amount / quantity` из уже созданного заказа Fragment: в `amount` комиссия API уже есть.
4. Для Premium можно ничего не задавать, если есть `API_TOKEN`: цены берутся из `GET /premium/packages`. Без ключа задайте `SHOP_PREMIUM_COST_3M`, `SHOP_PREMIUM_COST_6M` и `SHOP_PREMIUM_COST_12M`.
5. Оплата покупателя. Магазин только записывает уже подтверждённый `charge_id` через `POST /orders/{id}/pay`.
6. В `live` обязателен `SHOP_ADMIN_TOKEN`. Им закрыты оплата, выдача и баланс. Иначе любой запрос мог бы потратить баланс Fragment.

Получатель — username. Числовой `user_id` Fragment не принимает.

## Бот в Telegram

Покупатель открывает бота в Telegram, выбирает товар и username. Бот называет сумму. Fragment вызывается только после команды администратора `/paid`.

Токен бота берётся у [@BotFather](https://t.me/BotFather): команда `/newbot`, затем скопируйте выданный токен в `BOT_TOKEN`. Это не `API_TOKEN` и не `SHOP_ADMIN_TOKEN`.

```bash
SHOP_MODE=dry-run \
BOT_TOKEN='токен из BotFather' \
SHOP_MARKUP_PERCENT=20 \
SHOP_STAR_UNIT_COST=0.015 \
SHOP_PREMIUM_COST_3M=12 \
SHOP_PREMIUM_COST_6M=22 \
SHOP_PREMIUM_COST_12M=40 \
python3 -m shop.bot
```

В Telegram откройте своего бота и отправьте `/start`. В ответе будет ваш числовой id. Остановите бота и запустите снова, подставив этот id. Пока режим `dry-run`, `/paid` ничего не покупает на Fragment и только проверяет диалог.

```bash
SHOP_MODE=dry-run \
BOT_TOKEN='токен из BotFather' \
TELEGRAM_ADMIN_IDS='ваш_числовой_id' \
SHOP_MARKUP_PERCENT=20 \
SHOP_STAR_UNIT_COST=0.015 \
SHOP_PREMIUM_COST_3M=12 \
SHOP_PREMIUM_COST_6M=22 \
SHOP_PREMIUM_COST_12M=40 \
python3 -m shop.bot
```

Для живой выдачи замените режим на `SHOP_MODE=live` и добавьте `API_TOKEN`. Команда `/paid id_заказа` тогда покупает товар на Fragment. Сначала проверьте бота в `dry-run`.

Числа ниже — пример, не прайс Fragment. Подставьте свою наценку и свою себестоимость.

## HTTP-сервер

Тот же магазин без окна Telegram:

```bash
python3 -m unittest discover -s tests
SHOP_MARKUP_PERCENT=20 \
SHOP_STAR_UNIT_COST=0.015 \
SHOP_PREMIUM_COST_3M=12 \
SHOP_PREMIUM_COST_6M=22 \
SHOP_PREMIUM_COST_12M=40 \
SHOP_PORT=8000 \
python3 -m shop
```

Живая покупка:

```bash
SHOP_MODE=live \
API_TOKEN='ключ Fragment API' \
SHOP_ADMIN_TOKEN='длинный секрет' \
SHOP_MARKUP_PERCENT=20 \
SHOP_STAR_UNIT_COST=0.015 \
SHOP_WALLET=USDT \
SHOP_PORT=8000 \
python3 -m shop
```

Песочница: `FRAGMENT_API_BASE=https://sandbox.fragmentapi.com/api/v1/partner`.

Проверка заказа на 100 звёзд. В `live` к `pay` и `fulfill` добавьте заголовок `X-Admin-Token`.

```bash
curl -s http://127.0.0.1:8000/catalog

curl -s http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{"product_id":"stars_100","recipient_username":"username"}'

curl -s http://127.0.0.1:8000/orders/ORDER_ID/pay \
  -H 'Content-Type: application/json' \
  -d '{"charge_id":"payment-1"}'

curl -s http://127.0.0.1:8000/orders/ORDER_ID/fulfill \
  -H 'Content-Type: application/json' \
  -d '{}'
```

`fulfill` со статусом `delivering` и кодом 202 значит, что Fragment заказ принял, но ещё не довёл до `completed`. Повторите `fulfill`: магазин опросит `GET /orders/{id}`. Покупка до оплаты возвращает `not_paid`. Повтор той же выдачи использует тот же `Idempotency-Key`, это id заказа.

Сервер без аутентификации покупателя. Не публикуйте его в интернет. `API_TOKEN` остаётся только в окружении процесса.
