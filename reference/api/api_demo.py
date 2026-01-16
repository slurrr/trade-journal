# ruff: noqa
# pyright: ignore



# From Apex API docs 

# Sign request message by apiSecret
def sign(
        self,
        request_path,
        method,
        iso_timestamp,
        data,
):
    sortedItems=sorted(data.items(),key=lambda x:x[0],reverse=False)
    dataString = '&'.join('{key}={value}'.format(
        key=x[0], value=x[1]) for x in sortedItems if x[1] is not None)

    message_string = (
            iso_timestamp +
            method +
            request_path +
            dataString
    )

    hashed = hmac.new(
        base64.standard_b64encode(
            (self.api_key_credentials['secret']).encode(encoding='utf-8'),
        ),
        msg=message_string.encode(encoding='utf-8'),
        digestmod=hashlib.sha256,
    )
    return base64.standard_b64encode(hashed.digest()).decode()

# Generate apiKey
from apexomni.constants import APEX_OMNI_HTTP_MAIN, NETWORKID_OMNI_MAIN_ARB, NETWORKID_MAIN

print("Hello, Apex Omni")
priKey = "your eth private key"

client = HttpPrivate_v3(APEX_OMNI_HTTP_MAIN, network_id=NETWORKID_MAIN, eth_private_key=priKey)
configs = client.configs_v3()

zkKeys = client.derive_zk_key(client.default_address)
print(zkKeys)
print(zkKeys['seeds'])
print(zkKeys['l2Key'])
print(zkKeys['pubKeyHash'])

nonceRes = client.generate_nonce_v3(refresh="false", l2Key=zkKeys['l2Key'],ethAddress=client.default_address, chainId=NETWORKID_OMNI_MAIN_ARB)

regRes = client.register_user_v3(nonce=nonceRes['data']['nonce'],l2Key=zkKeys['l2Key'], seeds=zkKeys['seeds'],ethereum_address=client.default_address)

print(regRes['data']['apiKey']['key'])
print(regRes['data']['apiKey']['secret'])
print(regRes['data']['apiKey']['passphrase'])

time.sleep(10)
accountRes = client.get_account_v3()
print(accountRes)


# this is a sample function from another project

def sign_apex_request(method: str, url: str, data: Dict[str, str], signer) -> Dict[str, str]:
    """
    Generate ApeX headers using the SDK signer (HttpPrivateSign.sign).
    """
    api_key = os.getenv("APEX_API_KEY")
    api_passphrase = os.getenv("APEX_PASSPHRASE")
    if not all([api_key, api_passphrase]):
        raise RuntimeError("APEX_API_KEY and APEX_PASSPHRASE must be set in env")

    parsed = urlparse(url)
    request_path = parsed.path or "/"

    timestamp = str(int(time.time() * 1000))
    signature = signer.sign(request_path, method.upper(), timestamp, data)

    return {
        "APEX-API-KEY": api_key,
        "APEX-PASSPHRASE": api_passphrase,
        "APEX-SIGNATURE": signature,
        "APEX-TIMESTAMP": timestamp,
    }