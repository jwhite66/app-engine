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
import pprint

from google.appengine.api import urlfetch
from google.appengine.api import mail, memcache
from google.appengine.ext import db, deferred

import commands
import model
import paypal

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader('templates/'),
    extensions=['jinja2.ext.autoescape'],
    autoescape=True)


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
      'commands': AdminDashboardHandler.get_commands(),
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
      if user.occupation and user.employer:
        db.delete(missing_user_secondary)
      else:
        # missing_user_secondary.amountCents never gets updated, but
        # that's okay, because it won't change unless the user makes a
        # new pledge, which will cause their info to be updated, so
        # we'll go down the other fork in this if.
        users.append((user, missing_user_secondary.amountCents))
    users.sort(key=lambda (_, amt): amt, reverse=True)
    return users

  @staticmethod
  def get_commands():
    return [dict(name=c.NAME, url='/admin/command/' + c.SHORT_NAME)
            for c in commands.COMMANDS if c.SHOW]


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
  logging.info("Attempting to capture Paypal Billing Agreement " + id)
  pledge = model.Pledge.all().filter("paypalBillingAgreementID =", id).get()

  form_fields = {
    "METHOD": "DoReferenceTransaction",
    "REFERENCEID": id,
    "PAYMENTACTION": "Sale",
    "AMT": pledge.amountCents / 100,
  }
  rc, results = paypal.send_request(form_fields)

  if rc:
    pledge.paypalCapturedTransactionID = results['TRANSACTIONID'][0]
    pledge.captureError = None
    logging.info("Paypal Transaction " + id + " succeeded")

  else:
    logging.error("Paypal Transaction " + id + " failed")
    pledge.captureError = pprint.pformat(results)

  pledge.captureTime = datetime.datetime.utcnow()
  pledge.put()

  if rc:
    # Now cancel it
    form_fields = {
      "METHOD": "BillAgreementUpdate",
      "REFERENCEID": id,
      "BILLINGAGREEMENTSTATUS": "Canceled",
    }
    rc, results = paypal.send_request(form_fields)



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
      deferred.defer(PaypalCaptureOne, p.paypalBillingAgreementID, _queue="paypalCapture")
      self.response.write("<tr><td>" + p.email + "</td><td>" + str(p.amountCents / 100) +
           "</td><td>" + p.paypalBillingAgreementID + "</td></tr>")
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
  ('/admin/pledges.csv', PledgesCsvHandler),
  ('/admin/paypal.capture', PaypalCaptureHandler),
  ('/admin/?', AdminDashboardHandler),
] + COMMAND_HANDLERS, debug=False)
