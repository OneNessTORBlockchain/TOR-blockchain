# The 120-token fleet — one wallet, one token

Written 2026-10-28T18:31:19+00:00 by
`oneness_tor/services/token_fleet.py` on chain
`072316`
(node `http://127.0.0.1:8599`) —
**generation 2**.

The 120 assets are the **top 100 by market cap** plus the **top 20 Solana
memecoins**, fetched from key-less public providers with two fallbacks each
(`oneness_tor/services/market_feed.py`: CoinGecko → CoinPaprika → CoinLore
for the majors; CoinGecko's `solana-meme-coins` category → GeckoTerminal →
DexScreener for the memecoins).

Every token is a real `token_create` on the ledger: a freshly generated
Ed25519 wallet (funded from the founder) signs it and pays the chain's own
5 TOR fee, the on-chain supply is the provider's **total supply**, and the
logo is the provider's image — remembered in this node's logo cache at mint
and mirrored into `data/metadata/token-logos/`, so the token document, the
token table, the launcher, the wallet page and the explorer draw the same
local file at `/assets/tokens/<SYMBOL>.<ext>` (no third-party image host).

| generation | tokens | wallets | logos | supply minted | fee paid (TOR) |
| --- | --- | --- | --- | --- | --- |
| 2 | 120 | 120 | 120 | 421,990,690,155,595,478 | 600 |

Machine-readable twin: `data/metadata/token-fleet.json` — logo cache:
`data/metadata/token-images.json` — logo mirror:
`data/metadata/token-logos/` — market data cache:
`data/metadata/market-universe.json`. A superseded generation is frozen in
`data/metadata/token-fleet/gen-<n>.json` (never deleted, never merged).

## The 120 wallets, keys and tokens

`address`, `pubkey` and `mint address` are the public halves of each token:
the wallet's Ed25519 public key, the address derived from it, and the ledger
token id (`tor:<TICKER>`). The secret half stays in
`data/wallets/<wallet>.json`, which git ignores. Never copy that file into a
document, an issue or a chat.

| # | ticker | name | supply | price (USD) | market cap (USD) | wallet | address | pubkey | mint address | logo |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | BTC | Bitcoin | 20,104,884 | 80,421 | 1,616,844,962,652 | fleet121 | TOR1vf542vcwprvzfux5oypiamz65mgdixlqonion | ESEBJVKMFZ2ZeFkeB63WQQfDuuKqiCLf6FyErNbXDLdw | `tor:BTC` | [image](https://coin-images.coingecko.com/coins/images/1/large/bitcoin.png?1696501400) |
| 2 | ETH | Ethereum | 122,170,166 | 2,330.13 | 284,672,414,667 | fleet122 | TOR1kllte4xr5lildtp6e7necxuc75wsbi5bonion | 8K7KQZJPbtdmooPJ9DJm5bJHRDRRSmgTLhaNYPMiQ9NM | `tor:ETH` | [image](https://coin-images.coingecko.com/coins/images/279/large/ethereum.png?1696501628) |
| 3 | USDT | Tether | 187,295,207,856 | 0.998854 | 181,619,829,293 | fleet123 | TOR1iuxy4pbrcght3v45u2p4pepgdovyygryonion | Di1nkephzM8DUA23KPMqaGokV44KWWed2gApaYq34DYX | `tor:USDT` | [image](https://coin-images.coingecko.com/coins/images/325/large/Tether.png?1696501661) |
| 4 | BNB | BNB | 131,511,528 | 748.99 | 98,501,089,034 | fleet124 | TOR1uvjgiqz3sni27mxnrkanvkdapknkpqybonion | CAAtBEgvXsJM1vd42f4bUWM4uvouEwpepkYMptU6bZoZ | `tor:BNB` | [image](https://coin-images.coingecko.com/coins/images/825/large/bnb-icon2_2x.png?1696501970) |
| 5 | XRP | XRP | 99,985,589,443 | 1.31 | 82,957,856,366 | fleet125 | TOR1gmfhfowyvzkftp4r2zgagusu6wohgjrqonion | A55gvEGszdp39QRAH4xwK1aEPuPKtRTdwZXXEpvg4Dpk | `tor:XRP` | [image](https://coin-images.coingecko.com/coins/images/44/large/xrp-symbol-white-128.png?1696501442) |
| 6 | USDC | USDC | 71,055,245,213 | 0.999345 | 71,004,517,150 | fleet126 | TOR1gpvxnwljpaahfh2hr46zoeod3x7ylvv2onion | 7jWim6FaVs7HnSeFFmcUP7FTaqySYpz33VMKgC5WXGiG | `tor:USDC` | [image](https://coin-images.coingecko.com/coins/images/6319/large/USDC.png?1769615602) |
| 7 | SOL | Solana | 636,578,134 | 100.92 | 59,535,788,955 | fleet127 | TOR1hfhqqvhzla3fpl5li743aly2hu6j3dm7onion | 8jX4E3p5XXHLZr3rMu4BRtLVoD1AkLzwpKR1WzqEGV7i | `tor:SOL` | [image](https://coin-images.coingecko.com/coins/images/4128/large/solana.png?1718769756) |
| 8 | TRX | TRON | 95,017,831,960 | 0.330252 | 31,379,832,724 | fleet128 | TOR1cpmgwdrrvculynahwckmoukvqixvzx5ponion | HGPNYDGkCrvjM1ysiMEL9VtatLoUcSk9LH14HEUoFiup | `tor:TRX` | [image](https://coin-images.coingecko.com/coins/images/1094/large/photo_2026-04-13_09-59-16.png?1776048311) |
| 9 | ZEC | Zcash | 17,011,024 | 1,272.73 | 21,649,899,310 | fleet129 | TOR1t5ejrahdq4fwq3bie7mappx7ruood3rdonion | 7q3YUTyNjUMgvq1PnDDkmT4epwBXPd4cWLjQzxWarptF | `tor:ZEC` | [image](https://coin-images.coingecko.com/coins/images/486/large/Brandmark-Yellow_%281%29.png?1785810558) |
| 10 | HYPE | Hyperliquid | 951,538,273 | 79.93 | 19,136,009,008 | fleet130 | TOR1rwgtnzlf7znwvil2ojzdatbwvx4vzdfkonion | AYKwUqnuioPLDu1FE7GUSb2zD7QrcmpKbeDhEPjfg1pW | `tor:HYPE` | [image](https://coin-images.coingecko.com/coins/images/50882/large/hyperliquid.jpg?1729431300) |
| 11 | DOGE | Dogecoin | 156,514,392,860 | 0.079185 | 12,393,375,918 | fleet131 | TOR1j5u76guo34phxdjqlziqsns23d653wxsonion | DP9GtCqidHRrmnUUB4LP7HPhwmYaqRPJWDzvNbGpwYbw | `tor:DOGE` | [image](https://coin-images.coingecko.com/coins/images/5/large/dogecoin.png?1696501409) |
| 12 | XMR | Monero | 18,823,610 | 603.35 | 11,357,156,761 | fleet132 | TOR1afg4nkvi4wr7jk4cmphz4fblzul4mfykonion | 2LbE3SAvjXzuEhaHpAHMtyb3QQ2T4tmnZywwCREZZK1Q | `tor:XMR` | [image](https://coin-images.coingecko.com/coins/images/69/large/monero_logo.png?1696501460) |
| 13 | USDS | USDS | 9,858,794,954 | 0.99982 | 9,857,018,179 | fleet133 | TOR1hzjak5tqz6ezxkujy2eofbhwk2zokbc6onion | 6Z66VTdwKdD8vUVdvej8ucnFNN1XpCkmF2gQx2ZE8Prx | `tor:USDS` | [image](https://coin-images.coingecko.com/coins/images/39926/large/usds.webp?1726666683) |
| 14 | WBT | WhiteBIT Coin | 293,257,810 | 77.2 | 9,163,029,957 | fleet134 | TOR1tww5dubjvijyn45rh2g5astnqj6dblj6onion | Aw3EFwKfSV1oD1e28FYKYtJP6ty2cUWnKGHtPikvUvqJ | `tor:WBT` | [image](https://coin-images.coingecko.com/coins/images/27045/large/wbt_token.png?1696526096) |
| 15 | LINK | Chainlink | 1,000,000,000 | 11.53 | 8,841,077,330 | fleet135 | TOR1mytqwntebz7q47ml5ilftmlkstnctw4fonion | DLpaPmwkam9WBYiob9nPexo4HGjxbJLuK4QJyPzkgdew | `tor:LINK` | [image](https://coin-images.coingecko.com/coins/images/877/large/Chainlink_Logo_500.png?1760023405) |
| 16 | LEO | LEO Token | 985,239,504 | 9.4 | 8,649,898,375 | fleet136 | TOR14aegmjao5ztwd5tm362q6kpdiaqvi4q2onion | 4JMG2u7yi8wbGKw4x5hHVzRQMcsBNxCzTGAYEnDPgwSF | `tor:LEO` | [image](https://coin-images.coingecko.com/coins/images/8418/large/leo-token.png?1696508607) |
| 17 | ADA | Cardano | 45,000,000,000 | 0.202194 | 7,596,718,185 | fleet137 | TOR1cbqnypxoqjqgn4jokpcc4oloy7qdgen4onion | CSV9sEMPavauUNAiaehmExDuvYJo2edxq6PNzkKxrjh6 | `tor:ADA` | [image](https://coin-images.coingecko.com/coins/images/975/large/cardano.png?1696502090) |
| 18 | NEAR | NEAR Protocol | 1,310,187,816 | 4.6 | 6,030,263,614 | fleet138 | TOR1odbwknxdgjhsokvhb36ta27qxgpoqnf5onion | CwMnkJprPsXE3vLtEevgJTKMFprLzMbfgLLXP9PMcc78 | `tor:NEAR` | [image](https://coin-images.coingecko.com/coins/images/10365/large/near.jpg?1696510367) |
| 19 | XLM | Stellar | 50,001,786,837 | 0.170406 | 6,003,474,459 | fleet139 | TOR1vnwzpsgswelhedmaoojun3s3xoxsra2zonion | GstDFgjjZbmb7k2b1DTMZ1KrGAGcYAd2zZLfqHXbmjLL | `tor:XLM` | [image](https://coin-images.coingecko.com/coins/images/100/large/fmpFRHHQ_400x400.jpg?1735231350) |
| 20 | BCH | Bitcoin Cash | 20,109,109 | 289.78 | 5,827,165,955 | fleet140 | TOR1fzqqjetk2z2bwfdlosnlym6cwmx36rzwonion | 8wykKQiikvQfHkU7XBKbbHfuXW2HUUp2oG8dX4Q5nNFk | `tor:BCH` | [image](https://coin-images.coingecko.com/coins/images/780/large/bitcoin-cash-circle.png?1696501932) |
| 21 | RAIN | Rain | 1,142,404,738,215 | 0.00690376 | 4,907,395,451 | fleet141 | TOR1rbbcajcl5qbbcss6e6yjqpboi554zynbonion | 3s86v2w8ab2R5vBZ6yrKkPypkYHNNPJQXPRezH93Skpi | `tor:RAIN` | [image](https://coin-images.coingecko.com/coins/images/69134/large/Rain_logo_1_.png?1762952191) |
| 22 | LTC | Litecoin | 77,768,342 | 59.1 | 4,595,568,284 | fleet142 | TOR1tf3tjgtyewc2gz5bbnwbkmgg6m75ou6bonion | A4etQsmH6AT6q5vcdUujcf96WNF2eq7bw1jwycFp2orQ | `tor:LTC` | [image](https://coin-images.coingecko.com/coins/images/2/large/litecoin.png?1696501400) |
| 23 | USDE | Ethena USDe | 4,542,681,553 | 0.99937 | 4,539,819,509 | fleet143 | TOR1gzpbhlrwwvj5go6w767yempnb6gbs5sgonion | 4UMefJnoLw68Zh2LfSoXCqXejZPtNPCig7sC5KaJJebQ | `tor:USDE` | [image](https://coin-images.coingecko.com/coins/images/33613/large/usde.png?1733810059) |
| 24 | DAI | Dai | 4,524,076,235 | 0.999838 | 4,523,341,278 | fleet144 | TOR164f3qulbib2glse4gjotzlb3xjtlipwponion | s4zFekUhpS5fBZFCW7YWreC98cZnvJYAjeYS1va6y35 | `tor:DAI` | [image](https://coin-images.coingecko.com/coins/images/9956/large/Badge_Dai.png?1696509996) |
| 25 | UNI | Uniswap | 886,680,419 | 7.08 | 4,419,437,593 | fleet145 | TOR1kuzudtyscmta66lxi6z47tq4iawzofomonion | YQMCppbjDZkfcULYUXiNH1XD6wuULphw3gBfUmRSQp2 | `tor:UNI` | [image](https://coin-images.coingecko.com/coins/images/12504/large/uniswap-logo.png?1720676669) |
| 26 | CC | Canton | 39,947,250,149 | 0.099706 | 3,982,975,090 | fleet146 | TOR1bndc7rymol3ygvdjvtg5lnecwjydhgv5onion | 2ENByciYjbLyZoqi29ZfgtWgWDufX6DN4z7CFxxwRu3q | `tor:CC` | [image](https://coin-images.coingecko.com/coins/images/70468/large/Canton-Ticker_%281%29.png?1762826299) |
| 27 | AVAX | Avalanche | 470,636,405 | 8.86 | 3,932,798,913 | fleet147 | TOR1yriei35lccgtms6asl7dt4tneb5m73yaonion | FJmWcaxGeDk9GibbJ8BxPxzSFpFstinGyGPLDBRrRsk9 | `tor:AVAX` | [image](https://coin-images.coingecko.com/coins/images/12559/large/Avalanche_Circle_RedWhite_Trans.png?1696512369) |
| 28 | USD1 | USD1 | 3,899,189,305 | 0.998879 | 3,894,819,454 | fleet148 | TOR1d6bwzlkpshirffaqnlzaldktaawhhhkuonion | DUrYe6uFFH7Ska1FXNMgkjfPqxXzEchDknM7jq77Bdoi | `tor:USD1` | [image](https://coin-images.coingecko.com/coins/images/54977/large/USD1_1000x1000_transparent.png?1749297002) |
| 29 | SUI | Sui | 10,000,000,000 | 0.924274 | 3,806,409,019 | fleet149 | TOR1tgyjej7z5vme2ry7s5cyozuykn6xo5y6onion | 7seLBj7WC2AochG7So6ieFdjZay4gBDFyutR5t8EF1aX | `tor:SUI` | [image](https://coin-images.coingecko.com/coins/images/26375/large/sui-ocean-square.png?1727791290) |
| 30 | GRAM | Gram (prev. Toncoin) | 5,274,100,073 | 1.23 | 3,474,631,619 | fleet150 | TOR1unki64ssdjq76qvbhdy3c6kbj4us5qvkonion | 7JvDTSFLYZSv54xDUJZBBGBfGjZFrm3GMw25L8AMppai | `tor:GRAM` | [image](https://coin-images.coingecko.com/coins/images/17980/large/Gram_Circular_Badge.png?1781524778) |
| 31 | HBAR | Hedera | 50,000,000,000 | 0.075421 | 3,343,208,292 | fleet151 | TOR1mjr4edxucuxargvqjmr5g5hu7dh7qzkionion | C1j85usirgnFE7jS16BUF65anT4hRyQb2vrySjttThkk | `tor:HBAR` | [image](https://coin-images.coingecko.com/coins/images/3688/large/hbar.png?1696504364) |
| 32 | XAUT | Tether Gold | 827,418 | 4,033.79 | 3,263,311,933 | fleet152 | TOR1tos3ynst3a5sz47o2ikzfa5ls3plaecconion | 4Cr9XgFixfFgijKEssfmToekhGz6DZtdnfpXhVBwufQi | `tor:XAUT` | [image](https://coin-images.coingecko.com/coins/images/10481/large/logo.png?1774627372) |
| 33 | USDG | Global Dollar | 3,121,190,705 | 0.999754 | 3,120,421,380 | fleet153 | TOR1223rsp6v2jiugpp7gf4wtrkukmbrarzoonion | 6g5t7Qsizdk1WzvaV7qvYxKV8yivGKfXZz7TagRZfMrn | `tor:USDG` | [image](https://coin-images.coingecko.com/coins/images/51281/large/GDN_USDG_Token_200x200.png?1730484111) |
| 34 | PYUSD | PayPal USD | 3,022,355,774 | 0.999906 | 3,022,071,407 | fleet154 | TOR1fvqqehk4kyrkcg55wqfwbas4jx35faqvonion | F6BrD2CbzR6ozRmAhjNkKdRwELMY9vuAneCSWLocbxKz | `tor:PYUSD` | [image](https://coin-images.coingecko.com/coins/images/31212/large/PYUSD_Token_Logo_2x.png?1765987788) |
| 35 | TAO | Bittensor | 11,667,233 | 252.07 | 2,940,906,931 | fleet155 | TOR1lrevk6g5s5ho7tj74carmkf4co4p3hiuonion | 6YLF5VZ3kWXZs9fXH4VZ2GUDiGgkZLAQrUdqSByL6hX6 | `tor:TAO` | [image](https://coin-images.coingecko.com/coins/images/28452/large/ARUsPeNQ_400x400.jpeg?1696527447) |
| 36 | SHIB | Shiba Inu | 589,495,726,039,901 | 0.00000493 | 2,905,333,439 | fleet156 | TOR14tatjkcmqdjv2nmccufpct3molx2xy6sonion | D9X9d5FK3XvWyTLaZ2F6jEZ3isUqVgeQVFG2FJNz2aj2 | `tor:SHIB` | [image](https://coin-images.coingecko.com/coins/images/11939/large/shiba.png?1696511800) |
| 37 | QNT | Quant | 14,612,493 | 193.9 | 2,822,515,418 | fleet157 | TOR1oowhww43v6nsljysdyj4p32vwqvn6qesonion | 6WYLmnRoinpUrbTVhmqSnnnPgt7tv9FrEPYYY3YxoD6S | `tor:QNT` | [image](https://coin-images.coingecko.com/coins/images/3370/large/5ZOu7brX_400x400.jpg?1696504070) |
| 38 | CRO | Cronos | 99,006,553,088 | 0.053202 | 2,699,732,976 | fleet158 | TOR1peenz46d6kqknzffjlwsvpth6w67lep4onion | 3jERfBBNNEJQyWKKqKKKXULNL2iZxFeBTF7yjc7iF8Lx | `tor:CRO` | [image](https://coin-images.coingecko.com/coins/images/7310/large/cro_token_logo.png?1696507599) |
| 39 | ENA | Ethena | 15,000,000,000 | 0.195958 | 2,627,058,141 | fleet159 | TOR13wpaktbwlxqfa73ms5tc6uurl45jpsmdonion | DAgMKgVHWSsgfQdPfe1W9NyxHZQpTjgPhZmcJgyFdUyz | `tor:ENA` | [image](https://coin-images.coingecko.com/coins/images/36530/large/ethena.png?1711701436) |
| 40 | BTW | Bitway | 10,000,000,000 | 1.74 | 2,623,225,178 | fleet160 | TOR17lemuasdikpnlzy5hao73e5jy5xgo6kponion | CcQRBmdWXYzdiDHUYaXuaYaDwUQDR7W9Vcq3rtiYyCDv | `tor:BTW` | [image](https://coin-images.coingecko.com/coins/images/71205/large/BTW_Token_200x200.png?1786291907) |
| 41 | RLUSD | Ripple USD | 2,583,348,201 | 0.999995 | 2,583,334,488 | fleet161 | TOR1i32yv7xb2sezpqzgy37nqf3hrhr2qqkponion | 6dBJByBbpRvwWCmwy8Axw2aSi9u4yj4dpz5ADMSWcb3s | `tor:RLUSD` | [image](https://coin-images.coingecko.com/coins/images/39651/large/RLUSD_200x200_%281%29.png?1727376633) |
| 42 | USYC | Circle USYC | 2,080,845,018 | 1.14 | 2,376,075,060 | fleet162 | TOR1jh6ymb74f7m33jayyxpudbefmarockq7onion | HPd99h3PfQo5jX7MQVsjsRjGxyuwJxCijTEwcSb96BdP | `tor:USYC` | [image](https://coin-images.coingecko.com/coins/images/51054/large/Hashnote_SDYC_200x200.png?1730370965) |
| 43 | USDY | Ondo US Dollar Yield | 2,065,505,477 | 1.15 | 2,375,664,365 | fleet163 | TOR16jgva4tl2re3rpgqn2sucuhratjjinkxonion | 2wAeaqK7aDNHZHgRpt9826j9xr4E1Tiy8qew4U29Wy7f | `tor:USDY` | [image](https://coin-images.coingecko.com/coins/images/31700/large/usdy_%281%29.png?1696530524) |
| 44 | OKB | OKB | 21,000,000 | 109.93 | 2,308,435,883 | fleet164 | TOR1mkreq6wgq2xuhus2kfukomo4uaqarnikonion | EgTSx7H89gWevQyVkMus617EH8gdZKojpCmi9TNPyRDh | `tor:OKB` | [image](https://coin-images.coingecko.com/coins/images/4463/large/WeChat_Image_20220118095654.png?1696505053) |
| 45 | BUIDL | BlackRock USD Institutional Digital Liquidity Fund | 2,199,850,274 | 1 | 2,199,850,274 | fleet165 | TOR1lgkidb77gzxzs3nehrh2ekyeexuxkk6nonion | 9f8qRNC9dU1aCpgrgB4R6tSE7TZ9etqH8rZR53mTZ8E5 | `tor:BUIDL` | [image](https://coin-images.coingecko.com/coins/images/36291/large/blackrock.png?1711013223) |
| 46 | PEPE | Pepe | 420,690,000,000,000 | 0.00000484 | 2,038,200,471 | fleet166 | TOR1wolkii7tli6hmb72qkgyyn4ckalfpkluonion | CQGkKdNJLybQbohU23X7JrXfzvuqDQ43BqffMhHiJ3TY | `tor:PEPE` | [image](https://coin-images.coingecko.com/coins/images/29850/large/pepe-token.jpeg?1696528776) |
| 47 | AAVE | Aave | 16,000,000 | 131.99 | 2,037,476,190 | fleet167 | TOR1ymwbekfq5csageb353k3new7vcja4y2gonion | 2hSFm6V2Tt7aLRQUkw1hWhChtGvExqNnhH3CzpUaq9d5 | `tor:AAVE` | [image](https://coin-images.coingecko.com/coins/images/12645/large/aave-token-round.png?1720472354) |
| 48 | ONDO | Ondo | 10,000,000,000 | 0.415479 | 2,023,102,233 | fleet168 | TOR1e2towzt3i3fbm7swzio4xg7khecpqtfmonion | 7zW2b6n4q6kHGqSENTmMEgXVmeWBthCQ8A9vedghQsQH | `tor:ONDO` | [image](https://coin-images.coingecko.com/coins/images/26580/large/ONDO.png?1696525656) |
| 49 | PUMP | Pump.fun | 825,514,714,128 | 0.00418679 | 1,928,554,470 | fleet169 | TOR1brpqvzgjbfw2pqo3bv5n64cu3j4bzoaconion | G4iGpF79E61dQs2PJ3ucFJC1AtbmvifpLBBMd8D5oo4F | `tor:PUMP` | [image](https://coin-images.coingecko.com/coins/images/67164/large/pump.jpg?1751949376) |
| 50 | USDF | Falcon USD | 1,892,524,989 | 0.994987 | 1,883,038,679 | fleet170 | TOR1nnrf7y455otdc5fddrxwmnjaaotnp4bxonion | GhLtT92e5xwkSkiBRF34V6VbA57AArKdCCBYMWUWwUrb | `tor:USDF` | [image](https://coin-images.coingecko.com/coins/images/54558/large/ff_200_X_200.png?1740741076) |
| 51 | EURSAFO | Spiko Amundi Overnight Swap Fund (EUR) | 1,592,636,089 | 1.13 | 1,803,174,681 | fleet171 | TOR17vqrr3fklo2tnihzgdjsgpftpuev2jznonion | GUV2YC5SMvCUkTXCuQ5QZG8f68XUje8GdcpeeCaCtotJ | `tor:EURSAFO` | [image](https://coin-images.coingecko.com/coins/images/102172591/large/Fund_eurSAF0.png?1774104814) |
| 52 | PAXG | PAX Gold | 446,126 | 4,040.59 | 1,802,612,576 | fleet172 | TOR1u3hdx3rtnzshjfqozdnot4u4iwez6jzeonion | 6UTKvRRVumz3HCW6NagvTS95hoKeJ3ddSNaXQyPJ6SMr | `tor:PAXG` | [image](https://coin-images.coingecko.com/coins/images/9519/large/asset-paxg.png?1785284785) |
| 53 | ASTER | Aster | 7,789,910,941 | 0.703519 | 1,764,761,813 | fleet173 | TOR1ruwzy7nqxazyjkqzy2tpdom7aybrshxyonion | MWz95MMxBa3wFUd2bDpeSNymhmgbPVc5mSaXDWJwdQo | `tor:ASTER` | [image](https://coin-images.coingecko.com/coins/images/69040/large/_ASTER.png?1757326782) |
| 54 | DOT | Polkadot | 1,709,483,547 | 0.99504 | 1,701,003,803 | fleet174 | TOR1edcl7nj7vcb775elimsv6vtukevzngwwonion | BaMjXeRzemuqTMFr233J3RZ63j4ExB3ga9k6j3W7V7XH | `tor:DOT` | [image](https://coin-images.coingecko.com/coins/images/12171/large/polkadot.jpg?1766533446) |
| 55 | WLFI | World Liberty Financial | 100,000,000,000 | 0.052297 | 1,661,994,883 | fleet175 | TOR1uk5witzbctw7xz5oinm7vrfoqq52fwikonion | FixmVuonSMNggKHdb9VdZjN6ycuvW2m9GvQk8LfQsRqq | `tor:WLFI` | [image](https://coin-images.coingecko.com/coins/images/50767/large/wlfi.png?1756438915) |
| 56 | MNT | Mantle | 6,219,316,795 | 0.500723 | 1,653,387,837 | fleet176 | TOR1hbbdmvit54axhpqlstjdof2zuxpi5tn5onion | 66RLsgp8RuDMamjTTgeCaq4ZeztGnBcH9cd46qh3B3fj | `tor:MNT` | [image](https://coin-images.coingecko.com/coins/images/30980/large/MNT_Token_Logo.png?1765516974) |
| 57 | WLD | Worldcoin | 10,000,000,000 | 0.427966 | 1,611,667,042 | fleet177 | TOR1srlm62t7rbanr3dwdzudm2g4fvvclpyvonion | 4P9BjJa3uEEMfEJTcNhnDwroBdf7h4ByTnJ5d1722kRc | `tor:WLD` | [image](https://coin-images.coingecko.com/coins/images/31069/large/worldcoin.jpeg?1696529903) |
| 58 | MORPHO | Morpho | 1,000,000,000 | 2.2 | 1,544,593,104 | fleet178 | TOR1nk4cvrme7uanxtal4hrdub6npli57iuconion | 8FXXtoqisedn37qHa5b9rcP7Tj6APcWpWFNJG35Wj5nu | `tor:MORPHO` | [image](https://coin-images.coingecko.com/coins/images/29837/large/Morpho-token-icon.png?1726771230) |
| 59 | SKY | Sky | 23,452,431,916 | 0.065294 | 1,529,607,756 | fleet179 | TOR1kfk2odv3joegjjazl7pqtbzlnxasotmdonion | GsYtMkhJtY7XdFdZhdFxZzZhcjaC3PKN5Bh5tjNq2G6N | `tor:SKY` | [image](https://coin-images.coingecko.com/coins/images/39925/large/sky.jpg?1724827980) |
| 60 | USDD | USDD | 1,491,633,936 | 0.99786 | 1,478,866,177 | fleet180 | TOR15enjh3pxm4yzjwjfgoz2rw5qg23u2ahkonion | 2rAtQXT5JSDDtvJ6Ao2H39dYKthvuC51axbacsi2WAqY | `tor:USDD` | [image](https://coin-images.coingecko.com/coins/images/25380/large/UUSD.jpg?1696524513) |
| 61 | HTX | HTX DAO | 888,846,370,538,197 | 0.00000162 | 1,444,324,435 | fleet181 | TOR1uqoe4usl4luqikml5fl3kmqzd2uty6pqonion | Cs7f1zX6twoo74VEuEd7Bw2EkF1KCsiWQErjghULJwMG | `tor:HTX` | [image](https://coin-images.coingecko.com/coins/images/35491/large/Frame_1321318576.png?1708908626) |
| 62 | ICP | Internet Computer | 558,183,407 | 2.58 | 1,439,927,062 | fleet182 | TOR1w2abfj6fm2vjmes2zpotumk347bzamlgonion | GKXUBippmEPWv45UKVbX6AvPunSThJ4dGpoYGMnYZ8iK | `tor:ICP` | [image](https://coin-images.coingecko.com/coins/images/14495/large/Internet_Computer_logo.png?1696514180) |
| 63 | BFUSD | BFUSD | 1,320,000,000 | 0.998712 | 1,318,299,912 | fleet183 | TOR1he7qhjaptzhfbc7dhbbaizcxl3yggiexonion | J88HABsrixAhWMgz31nXXw4DiR29aBE9vcdnMRUtkdRJ | `tor:BFUSD` | [image](https://coin-images.coingecko.com/coins/images/68227/large/bfusd.png?1755132827) |
| 64 | BGB | Bitget Token | 907,875,974 | 1.86 | 1,301,672,756 | fleet184 | TOR16uq22pxmwy44m5pd47x4a3btkr2jpuveonion | GgaW91cQ1D7HhMPLwE6pNFChLJ5y7AA4gkZ2hqATD9pQ | `tor:BGB` | [image](https://coin-images.coingecko.com/coins/images/11610/large/Bitget_logo.png?1736925727) |
| 65 | USDGO | USDGO | 1,299,333,374 | 1 | 1,299,422,827 | fleet185 | TOR17do5zhjxrfazncevllhwu5clawpy7x5eonion | 6DcA98jHHqTXzPDrYeviJEnVSpGmRXDR7xoosRC2rCFa | `tor:USDGO` | [image](https://coin-images.coingecko.com/coins/images/102172077/large/USDGO_Primary_Icon.png?1789694084) |
| 66 | ETC | Ethereum Classic | 158,845,730 | 7.55 | 1,199,813,845 | fleet186 | TOR1qzfhab7h4ogi2zo3whxxsf3oybzitmmxonion | 2RbH8VQoBkAJFZgXnWavSx5stiz4Xeivdnos2nHUwHJf | `tor:ETC` | [image](https://coin-images.coingecko.com/coins/images/453/large/ethereum-classic-logo.png?1696501717) |
| 67 | VVV | Venice Token | 81,191,999 | 23.95 | 1,165,743,025 | fleet187 | TOR1a72nindtwnrpf3py4ribccyirqpvygzbonion | G9U2cmYHUrHWQTPgABUnBpoPp6NpeGwWgpSYG99LaWJa | `tor:VVV` | [image](https://coin-images.coingecko.com/coins/images/54023/large/VVV_Token_Transparent.png?1741856877) |
| 68 | ARB | Arbitrum | 10,000,000,000 | 0.166148 | 1,145,109,947 | fleet188 | TOR14moiqjmyiutnh7duse4dg547yr6vvsuqonion | DKTTM6DdhJ9zGGhcRwHMp6qj8wfpuWKLPxhJ4vwP7SGp | `tor:ARB` | [image](https://coin-images.coingecko.com/coins/images/16547/large/arb.jpg?1721358242) |
| 69 | JST | JUST | 8,018,019,892 | 0.140629 | 1,127,566,568 | fleet189 | TOR1dfdxvzs32i2dvvn3rikeugzf5zuseycqonion | 3KoXcfmL9kk48ECngbUp4Z8dmksZmVH5oDqRdHqVDQsT | `tor:JST` | [image](https://coin-images.coingecko.com/coins/images/11095/large/JUST.jpg?1696511035) |
| 70 | KCS | KuCoin | 142,155,021 | 7.58 | 1,058,491,419 | fleet190 | TOR1yee4r2hazb7cdsjgyyzzxdafl5gpdgiaonion | 4mFthjTbrjcKoLy2qjSJ9NobedUDpeXyCCTnXadPJKKD | `tor:KCS` | [image](https://coin-images.coingecko.com/coins/images/1047/large/sa9z79.png?1696502152) |
| 71 | GT | Gate | 116,942,144 | 9.64 | 1,009,414,837 | fleet191 | TOR15f36fg7qgmoub7vjg37dd7hjad5zxs4uonion | FdWALmm72xcvWKx24KVZajieFrM9pMFDYrzScFC9EU2Q | `tor:GT` | [image](https://coin-images.coingecko.com/coins/images/8183/large/200X200.png?1735246724) |
| 72 | KAS | Kaspa | 27,780,559,729 | 0.0355566 | 987,775,712 | fleet192 | TOR1kzbeqzszxj4mpayfsp7qacbmidb7p4clonion | 8feNSZBStTNtbU6Qqkrw8ojy3jT9wHMKB3cbzSDLp1CH | `tor:KAS` | [image](https://coin-images.coingecko.com/coins/images/25751/large/kaspa-icon-exchanges.png?1696524837) |
| 73 | POL | POL (ex-MATIC) | 10,639,710,753 | 0.091514 | 973,677,972 | fleet193 | TOR1yhuzhbggovf34yauuy62fmxvbhng4exwonion | HMiivG9YSvndXprE1qX4hGj6NF15yd1YczWwWeLXS59c | `tor:POL` | [image](https://coin-images.coingecko.com/coins/images/32440/large/pol.png?1759114181) |
| 74 | JUP | Jupiter | 6,861,485,968 | 0.279509 | 927,792,427 | fleet194 | TOR1id74ksscz7ia476a37vzy53a6blwzml7onion | 8oTDV7ypr1AaSfLLgLF3ReouY79AJXYPP33CoGawmw2v | `tor:JUP` | [image](https://coin-images.coingecko.com/coins/images/34188/large/jup.png?1704266489) |
| 75 | ALGO | Algorand | 9,074,052,511 | 0.101976 | 925,336,796 | fleet195 | TOR1by3iqqutoqi6p5pp27vlckp7kqupatrhonion | 2EqMcaxvCJ1b6TqwPRmaVxVygxiB31XEjCjLq7NX1odX | `tor:ALGO` | [image](https://coin-images.coingecko.com/coins/images/4380/large/download.png?1696504978) |
| 76 | ATOM | Cosmos Hub | 538,669,329 | 1.62 | 873,909,350 | fleet196 | TOR1bdrogilkkktkfm27ja7743if744mulshonion | 2fWfsG5tqDJ2RMeRBBoLTy4sBsGXL9kk4zQ9K9fZtSqg | `tor:ATOM` | [image](https://coin-images.coingecko.com/coins/images/1481/large/cosmos_hub.png?1696502525) |
| 77 | RENDER | Render | 533,536,415 | 1.66 | 863,589,944 | fleet197 | TOR1miznrc7o3435yufo4hp47o7ig7xhpryzonion | J3Z1pX7R7AgMXCEwMcMschVwXzmHoQC1FpthVBD5kG9u | `tor:RENDER` | [image](https://coin-images.coingecko.com/coins/images/11636/large/rndr.png?1696511529) |
| 78 | PI | Pi Network | 17,379,587,794 | 0.076177 | 860,549,969 | fleet198 | TOR1k3zw6jwpfwa7zaryw3wxqmc54hkjsn5wonion | ETXNsmqkEeHP4yxX2fbeh1NJnEuqRt2wRptE76LVTnLQ | `tor:PI` | [image](https://coin-images.coingecko.com/coins/images/54342/large/pi_network.jpg?1739347576) |
| 79 | OUSD | Open USD | 828,087,618 | 0.99885 | 827,135,090 | fleet199 | TOR1eq3uxgu2kz677npkt4imagfnkkf5hh43onion | Cu8x7LQrtoZ1vgUboEYbdZzDtkuZdq5UA8XCxKKQYk21 | `tor:OUSD` | [image](https://coin-images.coingecko.com/coins/images/102174264/large/open_standard.png?1782864430) |
| 80 | NEXO | NEXO | 1,000,000,000 | 0.797789 | 797,789,114 | fleet200 | TOR16szqga2vcnytrg4tjlvwn2wj77afd44oonion | 7yAZC6Kjz3a7rfQVxUmwgUTv6boyaGM7rJPYZP1Vg23q | `tor:NEXO` | [image](https://coin-images.coingecko.com/coins/images/3695/large/CG-nexo-token-200x200_2x.png?1730414360) |
| 81 | INJ | Injective | 100,000,000 | 7.75 | 775,134,231 | fleet201 | TOR1gsmcn5d6fox4booevsbwp46su3hxz3buonion | 2p4qxFwPcuQBDavprDXUS7wve8Ls54g2Cw9ixJfD7cZ8 | `tor:INJ` | [image](https://coin-images.coingecko.com/coins/images/12882/large/Other_200x200.png?1738782212) |
| 82 | FIL | Filecoin | 1,956,892,810 | 0.890747 | 746,879,626 | fleet202 | TOR1emwgcub2k3xyn52guoqiyuneeqkxgtakonion | 4f1Y6VSWNBthD5gBSs8mVZcqpsFaJEzrDVdu8gzcUR8x | `tor:FIL` | [image](https://coin-images.coingecko.com/coins/images/12817/large/filecoin.png?1696512609) |
| 83 | LIT | Lighter | 981,909,280 | 2.95 | 736,516,627 | fleet203 | TOR1m3ily4zo4t5qumrndyowvp2pc47a2iwjonion | zjsREAQkwLzhmqauNnsKwsDD2CjAySpTMpbsntRUSxJ | `tor:LIT` | [image](https://coin-images.coingecko.com/coins/images/71121/large/lighter.png?1765888098) |
| 84 | GHO | GHO | 699,000,000 | 0.998004 | 697,605,132 | fleet204 | TOR1ba3xls3sfcz654rjtc6p4fjybzdfgcjionion | 7r8H38HArTy877DrNiqXCtwXz2ChABC9SEJJNEVAmBZa | `tor:GHO` | [image](https://coin-images.coingecko.com/coins/images/30663/large/gho-token-logo.png?1720517092) |
| 85 | DASH | Dash | 12,880,096 | 53.13 | 684,232,737 | fleet205 | TOR1d32psspl43ybe2py4qne7m6ifqwb2sdlonion | 4fjqBcunMJSUU3axd1zFKHjRJiWBirJ5MK1mqrwtZC6V | `tor:DASH` | [image](https://coin-images.coingecko.com/coins/images/19/large/dash-logo.png?1696501423) |
| 86 | USTB | Invesco Short Duration US Government Securities Fund | 60,731,651 | 11.26 | 683,832,983 | fleet206 | TOR1nll6o3c5zaq5ylghgynbor26qzzdw53sonion | 7dUvA2qp3T7xCKdWg8Z62oAeaJs7raMGwmAfNcRpqXXn | `tor:USTB` | [image](https://coin-images.coingecko.com/coins/images/35012/large/Invesco_icon_lg.png?1780816895) |
| 87 | CAKE | PancakeSwap | 337,548,979 | 2.1 | 683,474,467 | fleet207 | TOR1b423btm442o37pvq4gprvp7vx6ifjmoeonion | 3EPsif8wNEGDNqpENcSgosFy95PLfQr7X4CrHAr9AJrN | `tor:CAKE` | [image](https://coin-images.coingecko.com/coins/images/12632/large/pancakeswap-cake-logo_%281%29.png?1696512440) |
| 88 | ZRO | LayerZero | 1,000,000,000 | 1.69 | 676,845,139 | fleet208 | TOR1zrysyqkozll5jwgqsja2j6ufjyjh76gconion | AfxEQM2ADxtJFDqqFkswxB9KUeCag1bAxTBHq5qdzkMz | `tor:ZRO` | [image](https://coin-images.coingecko.com/coins/images/28206/large/ftxG9_TJ_400x400.jpeg?1696527208) |
| 89 | NIGHT | Midnight | 24,000,000,000 | 0.04027181 | 668,810,114 | fleet209 | TOR1x2iuy7c22426u2sbgkcb7us2k7a2v6doonion | DNe7tGz9qvtvyxDyXdggEsmDvKPrgh5CpqXjMoCYjc1o | `tor:NIGHT` | [image](https://coin-images.coingecko.com/coins/images/71015/large/midnight.png?1765193080) |
| 90 | XDC | XDC Network | 38,065,686,425 | 0.03282774 | 654,804,662 | fleet210 | TOR1zhbodfchcuon7oqdxbpv4paccaql5h4conion | 2YUbmXSi9qiFDicu2BA54vCEbTMqGHWBQLpEBwz4Acpc | `tor:XDC` | [image](https://coin-images.coingecko.com/coins/images/2912/large/xdc-icon.png?1696503661) |
| 91 | ETHFI | Ether.fi | 998,535,999 | 0.653065 | 652,108,480 | fleet211 | TOR1pu75jx6yx2byunysg463vck6advhcjrlonion | CekN7KrmtEjNEgpSgED87WiuX6WF2jF6aBXBB2g22wgg | `tor:ETHFI` | [image](https://coin-images.coingecko.com/coins/images/35958/large/etherfi.jpeg?1710254562) |
| 92 | PEARL | Pearl | 2,100,000,000 | 1.77 | 638,233,676 | fleet212 | TOR13wzjizvubvl5mlwn7o6zttxin6h7f5r6onion | Fe7Sim514LT6Mwi4HQHBq9unE59HLbgS1TwJJoMcWB7y | `tor:PEARL` | [image](https://coin-images.coingecko.com/coins/images/102173580/large/pearl.png?1780324634) |
| 93 | APT | Aptos | 1,210,753,472 | 0.684228 | 604,602,502 | fleet213 | TOR16kycxn4d4e7dyd7wfbsxicb37dka3gq7onion | EFRCS8r73kwsE2kEpadh5EFwBAyaCRodiwHfpWxA5guf | `tor:APT` | [image](https://coin-images.coingecko.com/coins/images/26455/large/Aptos-Network-Profile-Picture_%281%29.png?1788259511) |
| 94 | AERO | Aerodrome Finance | 2,006,874,385 | 0.704376 | 574,031,475 | fleet214 | TOR17wccsqxddazsjbybcaui5pwhq4yhi7hqonion | 7duC8Bb3qdNNEpj6vaA1tEAYU4q9CruawDA26p5L6Zxm | `tor:AERO` | [image](https://coin-images.coingecko.com/coins/images/31745/large/token.png?1696530564) |
| 95 | JAAA | Janus Henderson Anemoy AAA CLO Fund | 539,282,699 | 1.054 | 568,406,661 | fleet215 | TOR1biu3uw5w6y7pzdqjrprig7echcpa6fpxonion | BwCW5FoWbJq43dPMxPA4rfUTaCs5k291W8y49zkjoB1o | `tor:JAAA` | [image](https://coin-images.coingecko.com/coins/images/70446/large/jaaa.png?1762078666) |
| 96 | STX | Stacks | 1,878,570,016 | 0.302079 | 567,475,857 | fleet216 | TOR16vlgy5mxlhlcfxmsgr5k4ouse72znxesonion | GARuEsUokmMJnj3rMTLHkX24nSEnLaaMNNH8vKpo4L78 | `tor:STX` | [image](https://coin-images.coingecko.com/coins/images/2069/large/Stacks_Logo_png.png?1709979332) |
| 97 | BDX | Beldex | 9,939,718,245 | 0.071963 | 566,458,249 | fleet217 | TOR1notnzzcodna2jk5vu4q444hc4cds5fqgonion | 9uf1SVvWrLEq6xT5DGHXj3KyohpvazWStTpy7QDZSD2s | `tor:BDX` | [image](https://coin-images.coingecko.com/coins/images/5111/large/Beldex.png?1696505631) |
| 98 | PYTH | Pyth Network | 10,000,000,000 | 0.070966 | 558,857,385 | fleet218 | TOR1ds4wrey2ehnkeuotnc3qvz3kfd37wypbonion | EdxhtepXRY2m56WcZsSNEeSZxLJcZBNCJszipeUXbMiD | `tor:PYTH` | [image](https://coin-images.coingecko.com/coins/images/31924/large/pyth.png?1701245725) |
| 99 | VET | VeChain | 85,985,041,177 | 0.00630292 | 541,956,429 | fleet219 | TOR14ubbzj7zjae43qhly4u5xmau4brsvwroonion | 6BqjhrEL281q3HeqPyNkMQ2Vk4twLghXybMCFjPNEF2R | `tor:VET` | [image](https://coin-images.coingecko.com/coins/images/1167/large/VET.png?1742383283) |
| 100 | USD0 | Usual USD | 541,906,906 | 0.998769 | 541,240,013 | fleet220 | TOR1ilmgjpgw6ld3nh4ipqbumfmrjf2u75skonion | 7AXJo3RWLznb4QF5JEkP18oibWnAAqpianoetd8jMMRz | `tor:USD0` | [image](https://coin-images.coingecko.com/coins/images/38272/large/USD0LOGO.png?1716962811) |
| 101 | BONK | Bonk | 87,994,310,635,503 | 0.00000306 | 269,382,233 | fleet221 | TOR1bnbs6yvvpcjhw5heyhq7khyvnmjzxmguonion | FGm9wTquETqhjyRV7pqMdUYnr5GrBdqgYdbBf8WZXrp6 | `tor:BONK` | [image](https://coin-images.coingecko.com/coins/images/28600/large/bonk.jpg?1696527587) |
| 102 | WIF | dogwifhat | 998,926,392 | 0.18034 | 180,146,787 | fleet222 | TOR1orx2gxbzobl5emjysj2ikuoqjz6mw3kxonion | 27J8rSEpCFfThAdtFzyHkc1uMxHBXJ6eg4q1qSDqtxZM | `tor:WIF` | [image](https://coin-images.coingecko.com/coins/images/33566/large/dogwifhat.jpg?1702499428) |
| 103 | USELESS | Useless Coin | 999,940,362 | 0.159113 | 159,103,805 | fleet223 | TOR1hm2molovyorqse7i6ucczf6f6ntqkf5ronion | BNaneRqEpz72iGaDj5is5rSYKbQ3r2k6EJ7z2hgbccqw | `tor:USELESS` | [image](https://coin-images.coingecko.com/coins/images/55684/large/coingeckoupdate.png?1755203747) |
| 104 | FARTCOIN | Fartcoin | 999,914,429 | 0.130472 | 130,461,264 | fleet224 | TOR1bl76mi7h2cv2kmz3jbghivvw6rgpv7oronion | E19Cc5zsMbFnzYzNXR8TbwGuWw1VbLQBWcKHNiqfY986 | `tor:FARTCOIN` | [image](https://coin-images.coingecko.com/coins/images/50891/large/fart.jpg?1729503972) |
| 105 | DOG | Dog (Bitcoin) | 100,000,000,000 | 0.00088905 | 88,905,339 | fleet225 | TOR16rfmrvhlc5z6h5tmrgchy4dn4y6bmqi6onion | 4cKx2ogFxyCJCwfKvP3agkSjEx1NDrdJ4vowi3qZsMiC | `tor:DOG` | [image](https://coin-images.coingecko.com/coins/images/37352/large/DOGGOTOTHEMOON.png?1714096969) |
| 106 | MELANIA | Melania Meme | 999,995,511 | 0.084963 | 84,962,811 | fleet226 | TOR1pv25s4ur2qug2eldjkvlnorm6o6jtc72onion | 3BcFMvNABQRJWX4vYBnXFax4bP6Dka4q27u2gZUaa7pq | `tor:MELANIA` | [image](https://coin-images.coingecko.com/coins/images/53775/large/melania-meme.png?1737329885) |
| 107 | BABYDOGE | Baby Doge Coin | 420,000,000,000,000,000 | 0 | 64,100,884 | fleet227 | TOR1vgbswhkblz45xstny73rqff5tpl32zklonion | 5EAcz3vwt5FTeYK9YLBT3AmsjrGUGvmDqMwPdQYrVXaa | `tor:BABYDOGE` | [image](https://coin-images.coingecko.com/coins/images/16125/large/babydoge.jpg?1696515731) |
| 108 | BAN | Comedian | 999,961,859 | 0.056955 | 56,953,185 | fleet228 | TOR15pgmr2w4sg7jkghicny6gksg2afo6uwwonion | FbLudQt49KnQuuQDD8pxwwo1gh4TbQFcbbASpiWA6zYu | `tor:BAN` | [image](https://coin-images.coingecko.com/coins/images/51100/large/ban.jpg?1730085215) |
| 109 | ZCAT | Anonymous Cat | 966,094,789 | 0.04975739 | 48,070,356 | fleet229 | TOR1anjkuew6yzfzv3zvay7su2n4xtxgdavhonion | 7fqeiVRr1E62ASyiCwDEjnGnzjaPn5YD9gqCGDst9P4U | `tor:ZCAT` | [image](https://coin-images.coingecko.com/coins/images/102176753/large/zcat.webp?1788637007) |
| 110 | BOME | BOOK OF MEME | 68,999,659,569 | 0.0006329 | 43,669,822 | fleet230 | TOR1oae7hq3yhvodtgj57ocf47hwj3t2l2haonion | AJZBFGpwyMeZ3MdieAvmdEEBookFvyYqyCbVgtM11Z3k | `tor:BOME` | [image](https://coin-images.coingecko.com/coins/images/36071/large/bome.png?1710407255) |
| 111 | POPCAT | Popcat | 979,978,670 | 0.04297663 | 42,116,183 | fleet231 | TOR1bnx2r7tmem3izzgqne4rfb5s5moxdnokonion | DtjCeavPVTF4RP7gTj4hjeFMRmzdeFHZfDeER1Kh5m8R | `tor:POPCAT` | [image](https://coin-images.coingecko.com/coins/images/33760/large/image.jpg?1702964227) |
| 112 | PNUT | Peanut the Squirrel | 999,840,950 | 0.04147245 | 41,465,849 | fleet232 | TOR1hsl3vkol6xmqekkwjylywv7s3fvrme7jonion | 61TKnSkdt2gUecgSYM5XCVHFzd5euzxtGEn3xgL3LC7k | `tor:PNUT` | [image](https://coin-images.coingecko.com/coins/images/51301/large/Peanut_the_Squirrel.png?1734941241) |
| 113 | MEW | cat in a dogs world | 88,888,888,889 | 0.00044315 | 39,390,832 | fleet233 | TOR16dm6hdflanqjg6n6uk2zokk55ebhmvflonion | 81sxRWNPuSWs7zU1MrFTeRCx9tPuidp557eXTxw6XBkZ | `tor:MEW` | [image](https://coin-images.coingecko.com/coins/images/36440/large/MEW.png?1711442286) |
| 114 | MOODENG | Moo Deng | 989,936,394 | 0.03825871 | 37,873,690 | fleet234 | TOR16zycvrbpaxd4lqlqhdr63nah5houl2rxonion | HkjUdhvnkGRU49oesxuqds2PjRGPEDaCiPjUbm8xzC1N | `tor:MOODENG` | [image](https://coin-images.coingecko.com/coins/images/50264/large/MOODENG.jpg?1726726975) |
| 115 | TROLL | TROLL | 998,745,164 | 0.03144002 | 31,400,571 | fleet235 | TOR1knvgf4ckvymi2rez4jvhwmkzua5rwyy2onion | 39Y2Djs67KAdqydN6PMTbjFfuJqpMKsX8JQQUndZChqY | `tor:TROLL` | [image](https://coin-images.coingecko.com/coins/images/55282/large/Untitled_design.png?1777015928) |
| 116 | ZEREBRO | Zerebro | 999,945,878 | 0.03018004 | 30,178,410 | fleet236 | TOR1boiig42og352hj5ajgob46gwiwrb7z7monion | 2QrvxDm6doAasaUY8o33JznkHvVT7fVcZBPavGV3T4mL | `tor:ZEREBRO` | [image](https://coin-images.coingecko.com/coins/images/51289/large/zerebro_2.png?1730588883) |
| 117 | NEET | Not in Employment, Education, or Training | 999,748,156 | 0.02985967 | 29,852,150 | fleet237 | TOR1gdj5nibubcni2qni6wcud4m2aahvoms6onion | CLKCwZBjD9qWFA1nw1ww2hxEL3RLefA763u6ux5JhmMf | `tor:NEET` | [image](https://coin-images.coingecko.com/coins/images/55411/large/neet.jpg?1745850587) |
| 118 | ANSEM | The Black Bull | 997,359,376 | 0.064826 | 27,170,492 | fleet238 | TOR14hgtpwiebwgxiekkmszmvuj6pkn6rqtwonion | 8edXryg3TNArKtn9RfgNb6JsgfgbKgL6wNYwKviPFeJV | `tor:ANSEM` | [image](https://coin-images.coingecko.com/coins/images/102174035/large/IMG_9497.jpeg?1782005339) |
| 119 | BIRB | Moonbirds | 1,000,000,000 | 0.073562 | 20,965,296 | fleet239 | TOR1mzzb252lpsz3kxswlpuqarnznotvasi3onion | 9P8a9RD2Q5tpTn2ob3mntnZ5TvSdA3ykmGC6Av9rSWxs | `tor:BIRB` | [image](https://coin-images.coingecko.com/coins/images/69732/large/birb-logo.jpg?1759413928) |
| 120 | THEROS | Theros | 206,519,866 | 0.232238 | 19,009,219 | fleet240 | TOR1sjmc52wk2hs32kjofpn7xukmzcsunyggonion | 4t9d6Wy8QCDRbSukHkZBy6zWvgtK7zpYEJYA9s64fgYq | `tor:THEROS` | [image](https://coin-images.coingecko.com/coins/images/102172193/large/theros.png?1772081894) |

## Superseded generations

A generation is superseded when the same tickers are minted again into
new wallets (a re-mint), because a ticker exists once per chain and the
on-chain supply cannot be edited. Each frozen record keeps the old
wallets, addresses, public keys and mint transactions for audit.

| generation | written | tokens | wallets | replaced here | frozen record |
| --- | --- | --- | --- | --- | --- |
| 1 | 2026-10-28T16:35:20+00:00 | 120 | 120 | 119 | `data/metadata/token-fleet/gen-01.json` |

## How to verify

```bash
GET /api/tokens/fleet        # this record, straight from the node
GET /api/public/tokens       # supply, owner, mint address and logo per token
GET /api/tokens/images       # the exact image URL each token is drawn with
POST /api/tokens/images/mirror  # copy every logo onto this node (admin)
GET /assets/tokens/BTC.png   # what the pages actually draw
GET /api/tokens/universe     # the market data and which provider answered
```

A replica shows the same 120 tokens after `POST /api/chain/sync/pull` — the
tokens are ordinary ledger state.

Prices and market caps describe the real-world asset a ticker refers to: they
are public metadata, **not** chain state, and every price this node quotes
on-chain is denominated in TOR.
