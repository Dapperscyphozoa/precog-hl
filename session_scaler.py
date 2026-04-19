"""Session risk scaler. UTC hour → risk multiplier.
London/NY peak = 1.0x. NY afternoon = 0.85x. Asia = 0.7x.
"""
import time

def get_mult():
    h = time.gmtime().tm_hour
    if 7 <= h < 12:   return 1.0   # London
    if 12 <= h < 17:  return 1.0   # NY overlap
    if 17 <= h < 21:  return 0.85  # NY afternoon
    return 0.7                      # Asia (21:00-07:00 UTC)

def session_name():
    h = time.gmtime().tm_hour
    if 7 <= h < 12:   return 'London'
    if 12 <= h < 17:  return 'NY'
    if 17 <= h < 21:  return 'NY_PM'
    return 'Asia'
