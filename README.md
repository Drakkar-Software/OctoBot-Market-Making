# OctoBot Market Making

Version [2.0.9](https://github.com/Drakkar-Software/OctoBot-Market-Making/blob/master/CHANGELOG.md)

Your automated market making strategy software to improve your crypto market liquidity.

- On more than 15 supported exchanges
- Using a fully transparent open source market making automation algorithm
- For free, with an open source trading bot

![octobot market making preview](docs/octobot-market-making-preview.gif)

## Market making for crypto projects

- Generate liquidity for your token on exchanges listing it.
- Protect your token: As OctoBot Market Making is free and transparent, you stay in control of your token, always.
- Your liquidity in your own hands: No third party to trust with your coins.

## Market making for individuals

- Profit from stable markets: extract profits from local ups and downs.
- Increase your account trading volume to access to higher fee tiers and reduce exchange fees.
- Earn exchange’s liquidity provider rewards by participating in liquidity providing campaigns
- Farm volume-based airdrops, like the 2025 Hyperliquid HYPE airdrop.

![octobot market making dashboard with buy and sell orders](docs/octobot-market-making-dashboard-with-buy-and-sell-orders.png)

## Installation

### Docker

```shell
pull drakkarsoftware/octobot-market-making:stable
```

### Python

```shell
git clone https://github.com/Drakkar-Software/OctoBot-Market-Making
cd OctoBot-Market-Making
python -m pip install -Ur requirements.txt
python start.py
```

## How it works

OctoBot Market Making is a distribution of [Octobot](https://github.com/Drakkar-Software/OctoBot), a free open source crypto trading robot, which is being actively developed since 2018.

It leverages the automated trading en strategy engine of OctoBot to create and maintain an order book according to your strategy configuration.

Simply configure your market making strategy details such as your exchange and pair, how many orders to include, the target bid-ask spread or even your coin reference price from another exchange.

![octobot market making strategy configuration](docs/octobot-market-making-strategy-configuration.png)

## What’s included

### Order book design

Configure your exchange ideal liquidity by specifying how many bids and asks must be included in your strategy and the price range your orders should cover.

### Order book maintenance

The algorithm automatically replaces filled orders and adapts the order book according to the current price of your traded pair.

### Arbitrage protection

OctoBot Market Making builds its order book according to a reference price of the pair to provide liquidity on. This reference price can be from the local exchange or from another exchange with more liquidity on this pair.

Using another exchange as reference price will synchronize your bot’s order book around the price of this pair on the reference exchange. As a result, the strategy will instantly cancel and replace any order that does not align with your reference exchange price, effectively preventing arbitrage opportunities when the reference exchange has a more up-to-date price.

### Paper trading

OctoBot Market Making comes with a built-in trading simulator which you can use to configure your strategy and test it before connecting your bot to a real exchange account

![octobot market making paper trading configuration](docs/octobot-market-making-paper-trading-configuration.png)

## Going further

OctoBot Market Making is the backbone of [OctoBot cloud Market Making](https://market-making.octobot.cloud/?utm_source=github&utm_medium=dk&utm_campaign=regular_open_source_content&utm_content=going_further_1), a self-service market making automation platform. 

If you enjoy OctoBot Market Making and wish to automate more complex market making strategies or if you are looking for more capabilities in your market making requirements, [OctoBot cloud Market Making](https://market-making.octobot.cloud/?utm_source=github&utm_medium=dk&utm_campaign=regular_open_source_content&utm_content=going_further_2) might be the right platform for you.