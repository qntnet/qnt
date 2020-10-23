import qnt.data as qndata
import qnt.stats as qnstats
import pandas as pd
import xarray as xr
import numpy as np
import qnt.forward_looking as qnfl
import time
from qnt.neutralization import neutralize
import datetime as dt
import qnt.exposure as qne

# data = qndata.crypto_load_data(assets=['foo'], max_date='2020-01-01')
# print(data)
# exit(0)

# data = qndata.index_load_data(assets=['foo'], max_date='2020-01-01')
# print(data)
# exit(0)

# data = qndata.futures_load_data(assets=['foo'], max_date='2020-01-01')
# print(data)
# exit(0)

# data = qndata.load_data( max_date='2020-11-01', tail=1)
# print(data)
# exit(0)

# data = qndata.index_major_load_data()
# print(data)
# exit(0)

assets = qndata.load_assets()

data = qndata.load_data(
    # assets=[a['id'] for a in assets[-150:]],
    # max_date='2020-03-01',
    tail=12*365,
    forward_order=True,
    dims=("time", "field", "asset"))


output = xr.ones_like(data.sel(field=qndata.f.CLOSE))
#output = qndata.sort_and_crop_output(output)
#output = neutralize(output, assets, 'industry')

# output *= 1

output.loc[{"time":slice('2017-01-01','2019-01-01')}] = np.nan
output.loc[{"time":slice('2018-01-01','2019-01-01'), "asset": "NASDAQ:MSFT"}] = 1
output = output.dropna('time', 'all')

print("First check.")

qndata.check_output(output, data)

print("Fix output.")

output = qndata.clean_output(output, data)

print("Second check.")

qndata.check_output(output, data)

print(output.to_pandas())
print(output[0, 0].item())



