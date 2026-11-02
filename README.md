# Магазин звёзд и Premium в Telegram-боте

Локальный каркас магазина. Он проводит заказ от выбора товара до выдачи и не ходит в Telegram, пока не включён живой режим.

По умолчанию `SHOP_MODE=dry-run`: Premium не отправляется в Telegram, а в заказ записывается точный вызов `giftPremiumSubscription`.

## Что умеет официальный Bot API

Premium можно подарить методом [`giftPremiumSubscription`](https://core.telegram.org/bots/api#giftpremiumsubscription). С баланса бота списывается фиксированная цена:

| Срок | Звёзды бота |
| --- | --- |
| 3 месяца | 1000 |
| 6 месяцев | 1500 |
| 12 месяцев | 2500 |

Другие сроки и цены API не принимает.

Пакет звёзд на баланс пользователя бот зачислить не может. Отдельного метода «отправить N Stars пользователю» нет. [`sendGift`](https://core.telegram.org/bots/api#sendgift) тоже не замена: получатель не может конвертировать такой подарок в звёзды. Поэтому пакеты звёзд есть в каталоге, но выдача завершается ошибкой `stars_not_deliverable`.

Баланс бота для подарков Premium пополняется платежами пользователей в валюте `XTR` или покупкой звёзд в самом Telegram. Купить звёзды за деньги вызовом Bot API нельзя.

## Что нужно снаружи этого репозитория

1. Бот в [@BotFather](https://t.me/BotFather) и токен в `BOT_TOKEN`. Токен в репозиторий не кладётся.
2. Баланс звёзд у бота не меньше цены выбранного Premium. Проверка: `getMyStarBalance`.
3. Оплата покупателя. Этот каркас только принимает уже подтверждённый `charge_id`. Эквайринг, крипто-провайдер или счёт подключаются отдельно и вызывают `POST /orders/{id}/pay` после успешного платежа.
4. Числовой `user_id` получателя. Username для `giftPremiumSubscription` не подходит.
5. Для живой выдачи: `SHOP_MODE=live` и `BOT_TOKEN`. Без токена заказ остаётся оплаченным и не помечается выданным.

## Локальная проверка

```bash
python3 -m unittest discover -s tests
SHOP_PORT=8000 python3 -m shop
```

Полезные адреса: `GET /health`, `GET /catalog`, `POST /orders`, `POST /orders/{id}/pay`, `POST /orders/{id}/fulfill`.

Пример Premium на 3 месяца:

```bash
curl -s http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{"product_id":"premium_3m","recipient_user_id":123456789}'
```

Дальше подтвердите оплату своим `charge_id` и вызовите `/fulfill`. В `dry-run` ответ содержит параметры подарка, но Telegram не вызывается.
