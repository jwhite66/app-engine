"""Helpers for retriving data from Paypal."""

import logging
import model
import urlparse
import urllib
import pprint

from google.appengine.api import urlfetch
def send_request(fields):
    config = model.Config.get()

    fields["VERSION"] = "113"
    fields["USER"] =  config.paypal_user
    fields["PWD"] =  config.paypal_password
    fields["SIGNATURE"] = config.paypal_signature

    form_data = urllib.urlencode(fields)

    result = urlfetch.fetch(url=config.paypal_api_url, payload=form_data, method=urlfetch.POST,
                headers={'Content-Type': 'application/x-www-form-urlencoded'})
    result_map = urlparse.parse_qs(result.content)

    if 'ACK' in result_map:
        if result_map['ACK'][0] == "Success":
            return (True, result_map)
   
        logging.warning("Paypal returned an error:")
        logging.warning(pprint.pformat(result_map))
        return (False, result_map)

    logging.warning("Could not contact Paypal:")
    logging.warning(result.content)
    return False, result.content
