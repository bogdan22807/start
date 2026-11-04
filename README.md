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

На `/start` бот приветствует человека по имени и ставит под текстом кнопку «Купить звезды». Кнопки «Открыть магазин» и «Магазин» больше не ставятся: они не открывали оплату.

Кнопка ведёт на магазин, где работают банковская карта и Crypto Bot. Если `WEBAPP_URL` пустая, это адрес:

https://start-bogdan22807s-projects.vercel.app

Эту же ссылку можно вставить в [@BotFather](https://t.me/BotFather) как Web App. Основной экран чёрный: синие переключатели, золотые звёзды, пакеты 50, 100, 250, 500, 1 000 и 2 500. Своё число — от 50 до 100 000. Premium: 3 месяца за 14.99 USDT, 6 за 19.99 USDT, 12 за 36.24 USDT. Получатель переключается между «Себе» и «Другу». Кнопка темы переключает чёрный и белый экран. «Оплатить» открывает одну банковскую карту с подписью Visa и Mastercard и крипту: Crypto Bot и Heleket. Списывают деньги Crypto Bot и банковская карта. В окне оплаты написано, что оплата Heleket временно недоступна.

## Оплата через Crypto Bot

Кнопка Crypto Bot создаёт счёт USDT. Когда Crypto Pay подтверждает оплату, магазин покупает звёзды или Premium на Fragment и отправляет их на указанный username. Цена считается на сервере по тем же суммам, что на экране. Повтор той же оплаты не создаёт вторую покупку.

В Vercel нужны `CRYPTO_PAY_TOKEN` (токен Crypto Pay из @CryptoBot, не токен бота) и `FRAGMENT_API_KEY`. Деньги покупателя остаются на балансе Crypto Bot. Звёзды и Premium магазин покупает отдельно с баланса Fragment: сначала `SHOP_WALLET` (по умолчанию USDT), а если там пусто — со второго баланса, TON или USDT. `API_TOKEN` с видом `123456789:AA...` остаётся токеном бота и в Fragment не отправляется. Если задано `SHOP_MODE=dry-run`, счёт не создаётся. Без этой переменной или со значением `live` оплата настоящая.

Вебхук в приложении Crypto Pay: `https://start-bogdan22807s-projects.vercel.app/api/cryptobot`. Пока вебхук не указан, магазин отправляет уже оплаченные покупки, когда его снова открывают. После оплаты кнопка «Вернуться» снова открывает магазин.

## Оплата картой

Кнопка «Банковская карта» создаёт счёт Monobank. Сумма в гривнах считается из цены в USDT по курсу доллара НБУ и показывается до оплаты. Если у мерчанта счета в долларах, в Vercel можно поставить `MONOBANK_CCY=840`: тогда спишется та же сумма в долларах, без курса. По умолчанию валюта `980` (гривна).

Токен эквайринга читается из `MONOBANK_TOKEN`. Также подходят имена `MONO_TOKEN`, `MONOBANK_API_KEY` и `MONOBANK_API_TOKEN`. Значение из кабинета не нужно никуда вставлять в код. Покупка уходит только когда Monobank сам отвечает статусом `success`, сумма совпадает со счётом и подпись уведомления сходится. Адрес уведомления передаётся при создании счёта: `https://start-bogdan22807s-projects.vercel.app/api/monobank`. Пока клиент не вернулся в магазин, тот же счёт проверяется при повторном открытии.

Токен бота берётся у BotFather командой `/newbot` и кладётся в `BOT_TOKEN`. Если это же значение уже сохранено как `API_TOKEN` и выглядит как `123456789:AA...`, бот возьмёт его оттуда.

```bash
BOT_TOKEN='токен из BotFather' python3 -m shop.bot
```

Пока в терминале нет строки `telegram bot polling`, сообщение `/start` никуда не доходит. Кнопка меню «Магазин» при этом ставится в Telegram отдельно и открывает ссылку выше.

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
