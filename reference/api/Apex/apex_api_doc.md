# Api Documentation

## REST Base Endpoint

Mainnet:
https://omni.apex.exchange/api/

## Websocket Endpoint

Timestamp = Current Unix Timestamp

Mainnet:
Public Websocket API
wss://quote.omni.apex.exchange/realtime_public?v=2&timestamp=1661415017232

Private Websocket API
wss://quote.omni.apex.exchange/realtime_private?v=2&timestamp=1661415017233

## Query path and Parameters

- You must include ApeX Omni's version number in the url path, e.g. v3.
- The parameter "url path" is not case-sensitive and lower case letters will be displayed by default. The en-dash (-) can be used as spaces between individual words.
- The HTTP endpoint parameters are in camel case, opening with lower case letters.
- Due to different time zones with time parameters, please use int64 across all instances by default.
- Randomly generated id and numbers may be long in length and considering that the js display may be intercepted, these will be reflected as a string by default
- For customized request headers, please enter key in upper case and use the en-dash (-) as spaces between individual words.

**Http Get Request**

Please add parameters to the query path as:

https://omni.apex.exchange/api/v3/transfers?limit=100&page=1&currencyId=USDC&chainIds=1

**Http Post Request**

Enter order data in body, in the content-type x-www-form-urlencoded. You do not need to encode your data. as l2Key=xxx&ethAddress=yyy&chainId=zzz

All requests made to private endpoints must be authenticated. The following signatures must be used for authentication:

## ApiKey Signature

- An apiKey signature is required for all private API endpoints
- Users can obtain and save their public and private apiKey via wallet signature verification on ApeX Omni desktop or app
- API trading users can utilize python-sdk to generate the private and public key pair for apiKey and save them for future API requests, refer to python sdk
- Signature content message includes timeStamp + method + path + dataString

  - message = timeStamp + method + path + dataString

- Get Requests: dataString content is not required, append query parameters in the path

  - message = timeStamp + method + path

- Post Requests: Request body is saved in dataString content. To ensure proper sorting for parameters within the request body, parameters will be sorted by alphabetical order, refer to python sdk

- Place signature content, apiKey and related information in the request header

    | Parameter        | Position | Type   | Type | Comment              |
    |------------------|----------|--------|------|----------------------|
    | APEX-SIGNATURE   | header   | string | true | signstr              |
    | APEX-TIMESTAMP   | header   | string | true | request timeStamp    |
    | APEX-API-KEY     | header   | string | true | key                  |
    | APEX-PASSPHRASE  | header   | string | true | passphrase           |
