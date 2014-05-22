import calendar
import csv
import jinja2
import json
import logging
import os
import urllib
import urllib2
import webapp2
import urlparse
import datetime

from google.appengine.api import urlfetch
from google.appengine.api import mail, memcache
from google.appengine.ext import db, deferred

import commands
import model

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader('templates/'),
    extensions=['jinja2.ext.autoescape'],
    autoescape=True)


class SetSecretsHandler(webapp2.RequestHandler):
  def get(self):
    s = model.Secrets.get()
    if s:
      self.response.write('Secrets already set. Delete them before reseting')
      return

    self.response.write("""
    <form method="post" action="">
      <label>Stripe public key</label>
      <input name="stripe_public_key"><br>
      <label>Stripe private key</label>
      <input name="stripe_private_key"><br>
      <h2>Sandbox Paypal credentials</h2>
      <label>Sandbox Paypal API username</label>
      <input name="paypal_sandbox_user"><br>
      <label>Sandbox Paypal API password</label>
      <input name="paypal_sandbox_password"><br>
      <label>Sandbox Paypal API signature</label>
      <input name="paypal_sandbox_signature"><br>
      <h2>Real Paypal credentials</h2>
      <label>Paypal API username</label>
      <input name="paypal_user"><br>
      <label>Paypal API password</label>
      <input name="paypal_password"><br>
      <label>Paypal API signature</label>
      <input name="paypal_signature"><br>
      <input type="submit">
    </form>""")

  def post(self):
    model.Secrets.update(
      stripe_public_key=self.request.get('stripe_public_key'),
      stripe_private_key=self.request.get('stripe_private_key'),
      paypal_sandbox_user=self.request.get('paypal_sandbox_user'),
      paypal_sandbox_password=self.request.get('paypal_sandbox_password'),
      paypal_sandbox_signature=self.request.get('paypal_sandbox_signature'),
      paypal_user=self.request.get('paypal_user'),
      paypal_password=self.request.get('paypal_password'),
      paypal_signature=self.request.get('paypal_signature'))


class AdminDashboardHandler(webapp2.RequestHandler):
  def get(self):
    users = AdminDashboardHandler.get_missing_data_users()

    pre_sharding_total = 0
    post_sharding_total = 0
    for p in model.Pledge.all():
      if p.model_version >= 2:
        post_sharding_total += p.amountCents
      else:
        pre_sharding_total += p.amountCents

    template = JINJA_ENVIRONMENT.get_template('admin-dashboard.html')
    self.response.write(template.render({
      'missingUsers': [dict(email=user.email, amount=amt/100)
                       for user, amt in users],
      'totalMissing': sum(v for _, v in users)/100,
      'preShardedTotal': pre_sharding_total,
      'postShardedTotal': post_sharding_total,
      'shardedCounterTotal': model.ShardedCounter.get_count('TOTAL'),
    }))

  # Gets all the users with missing employer/occupation/targeting data
  # who gave at least $200 when we were on wordpress. If a user has
  # since updated their info, delete that user's record in the
  # MissingDataUsersSecondary model.
  #
  # Returns list of (User, amountCents) tuples.
  @staticmethod
  def get_missing_data_users():
    users = []
    for missing_user_secondary in model.MissingDataUsersSecondary.all():
      user = model.User.get_by_key_name(missing_user_secondary.email)

      # If they've added their info, delete them.
      if user.occupation and user.employer and user.target:
        db.delete(missing_user_secondary)
      else:
        # missing_user_secondary.amountCents never gets updated, but
        # that's okay, because it won't change unless the user makes a
        # new pledge, which will cause their info to be updated, so
        # we'll go down the other fork in this if.
        users.append((user, missing_user_secondary.amountCents))

    return users


class PledgesCsvHandler(webapp2.RequestHandler):
  def get(self):
    self.response.headers['Content-type'] = 'text/csv'
    w = csv.writer(self.response)
    w.writerow(['time', 'amount'])
    for pledge in model.WpPledge.all():
      w.writerow([str(pledge.donationTime), pledge.amountCents])
    for pledge in model.Pledge.all():
      w.writerow([str(pledge.donationTime), pledge.amountCents])


def PaypalCaptureOne(id):
  logging.info("Attempting to capture Paypal Transaction " + id)
  config = model.Config.get()

  trans_key = db.Key.from_path('Pledge', 'transKey', 'paypalTransactionID', id)
  pledge = model.Pledge.all().filter("paypalTransactionID", id).get()

  form_fields = {
    "VERSION": "113",
    "USER": config.paypal_user,
    "PWD": config.paypal_password,
    "SIGNATURE": config.paypal_signature,
    "METHOD": "DoCapture",
    "AUTHORIZATIONID": pledge.paypalTransactionID,
    "COMPLETETYPE": "Complete",
    "AMT": pledge.amountCents / 100,
  }
  form_data = urllib.urlencode(form_fields)

  result = urlfetch.fetch(url=config.paypal_api_url, payload=form_data, method=urlfetch.POST,
              headers={'Content-Type': 'application/x-www-form-urlencoded'})

  result_map = urlparse.parse_qs(result.content)

  pledge.captureStatus = result_map['ACK'][0]
  pledge.captureTime = datetime.datetime.utcnow()

  if result_map['ACK'][0] == 'Success':
    pledge.paypalCapturedTransactionID = result_map['TRANSACTIONID'][0]
    logging.info("Paypal Transaction " + id + " succeeded")
  else:
    logging.error("Paypal Transaction " + id + " failed:")
    logging.error(result.content)

  pledge.put()

class PaypalCaptureHandler(webapp2.RequestHandler):
  def get(self):
    self.response.write("""
    <form method="post" action="">
      <h1>Warning: This will collect payment from Paypal customers.  Do not use lightly.</h1>
      <label>Maximum number of pledges to collect:</label>
      <input name="count"><br>
      <input type="submit">
    </form>""")
    q = model.Pledge.all()
    q.filter("captureTime = ", None)
    self.response.write("<p> Paypal pledges ready to collect: " +
      str(q.count()) + "</p>")


  def post(self):
    count = self.request.get("count")
    if not count:
      self.error(400)
      self.response.write("Error: must limit number of captures.")
      return

    j = json.load(open('config.json'))
    if not 'allowCapture' in j or not j['allowCapture']:
      self.error(400)
      self.response.write("Error: capture not allowed.")
      return

    q = model.Pledge.all()
    q.order("donationTime")
    q.filter("captureTime = ", None)

    total = 0
    queued = 0

    self.response.write("<table><tr><th>email</th><th>amount</th><th>transid</th></tr>")

    for p in q.run(limit=int(count)):
      # Queue this record for capture
      deferred.defer(PaypalCaptureOne, p.paypalTransactionID, _queue="paypalCapture")
      self.response.write("<tr><td>" + p.email + "</td><td>" + str(p.amountCents / 100) +
           "</td><td>" + p.paypalTransactionID + "</td></tr>")
      total += p.amountCents
      queued += 1

    self.response.write("</table><p>" + str(queued) + " pledges queued; total of $" + str(total / 100))


def MakeCommandHandler(cmd):
  """Takes a command and returns a route tuple which allows that command
     to be executed.
  """
  class H(webapp2.RequestHandler):
    def get(self):
      self.response.write("""
      <h1>You are about to run command "{}". Are you sure?</h1>
      <form action="" method="POST">
      <button>Punch it</button>
      </form>""".format(cmd.NAME))

    def post(self):
      deferred.defer(cmd.run)
      self.response.write('Command started.')

  return ('/admin/command/' + cmd.SHORT_NAME, H)


COMMAND_HANDLERS = [MakeCommandHandler(c) for c in commands.COMMANDS]

app = webapp2.WSGIApplication([
  ('/admin/set_secrets', SetSecretsHandler),
  ('/admin/pledges.csv', PledgesCsvHandler),
  ('/admin/paypal.capture', PaypalCaptureHandler),
  ('/admin/?', AdminDashboardHandler),
] + COMMAND_HANDLERS, debug=False)
