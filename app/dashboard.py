from flask import Blueprint, session, render_template, request, flash, redirect, url_for, escape
import functools
import arrow
from .auth import login_required
from .db import get_db
from .common import flasher

bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')

@bp.route('/')
@login_required
def index():
    #return 'hello, ' + session['email']
    metrics = {'applicants': {}, 'votes': {}}
    conn = get_db()
    c = conn.cursor()

    # get the applicant metrics
    c.execute('SELECT COUNT(id) FROM Applicants')
    metrics['applicants']['all'] = c.fetchone()[0]
    c.execute('SELECT COUNT(id) FROM Applicants WHERE verified=1')
    metrics['applicants']['verified'] = c.fetchone()[0]
    c.execute('SELECT COUNT(id) FROM Applicants WHERE completed=1')
    metrics['applicants']['completed'] = c.fetchone()[0]

    # get vote metrics
    c.execute('SELECT COUNT(id) FROM Votes')
    metrics['votes']['all'] = c.fetchone()[0]
    c.execute('SELECT COUNT(id) FROM Votes WHERE rating=1')
    metrics['votes']['1'] = c.fetchone()[0]
    c.execute('SELECT COUNT(id) FROM Votes WHERE rating=2')
    metrics['votes']['2'] = c.fetchone()[0]
    c.execute('SELECT COUNT(id) FROM Votes WHERE rating=3')
    metrics['votes']['3'] = c.fetchone()[0]
    c.execute('SELECT COUNT(id) FROM Votes WHERE rating=4')
    metrics['votes']['4'] = c.fetchone()[0]
    c.execute('SELECT COUNT(id) FROM Votes WHERE rating=5')
    metrics['votes']['5'] = c.fetchone()[0]
    c.execute('SELECT DISTINCT author, author_email FROM Votes')
    author_names = c.fetchall()
    authors = []
    if author_names is None:
        author_names = []
    for author in author_names:
        c.execute(
            'SELECT COUNT(id), AVG(rating) FROM Votes WHERE author_email=?',
            (author['author_email'],)
        )
        row = c.fetchone()
        authors.append({
            'name': author['author'],
            'email': author['author_email'],
            'count': row[0],
            'average': '{:.2}'.format(row[1])
        })

    authors.sort(key=lambda a: a['count'])
    if authors is not None:
        authors.reverse()
    metrics['votes']['authors'] = authors

    return render_template(
        'dashboard/index.html',
        session=session,
        metrics=metrics
    )

@bp.route('/admin')
@login_required
def admin():
    return render_template('dashboard/admin.html', session=session)

@bp.route('/table')
@login_required
def table():
    # parse query params
    sort = request.args.get('sort')
    reverse = request.args.get('reverse') is not None
    sorts_available=['mongo_id', 'verified', 'completed', 'name', 'email', 'school']
    if sort is None or (not sort in sorts_available):
        sort = 'mongo_id'

    # retreive from db
    c = get_db().cursor()

    # OK SO THIS LOOKS REALLY BAD BUT
    # we have already vetted our sort as explicitly part of a small number of columns, and SQLite does not let us interpolate the field name
    c.execute('''
        SELECT * FROM Applicants ORDER BY {} {}
    '''.format(sort, 'DESC' if reverse else 'ASC'))
    applicants = c.fetchall()

    out = []
    for applicant in applicants:
        applicant = dict(applicant)
        c.execute('''
            SELECT
                COUNT(app_id), AVG(rating)
            FROM Votes
            WHERE
                app_id=?
        ''', (applicant['mongo_id'],))

        row = c.fetchone()
        applicant['ratings'] = row[0]
        if row[1] is not None:
            applicant['score'] = '{:.3}'.format(row[1])
        else:
            applicant['score'] = ''
        out.append(applicant)

    # hack, but we need to be able to sort by these virtual fields too
    sort = request.args.get('sort')
    if sort == 'ratings' or sort == 'score':
        out.sort(key=lambda k: k[sort])
        if reverse:
            out.reverse()

    return render_template(
        'dashboard/table.html',
        session=session,
        rows=out,
        sort=sort,
        sort_reverse=reverse
    )

@bp.route('/applicant/<mongo_id>')
@login_required
def applicant(mongo_id):
    c = get_db().cursor()

    c.execute('SELECT * FROM Applicants WHERE mongo_id = ?', (mongo_id,))

    row = c.fetchone()
    if row is None:
        flasher('No such user with ID {}!'.format(mongo_id), color='danger')
        return redirect(url_for('dashboard.table'))

    # make sure newlines work
    essay       = str(escape(row['essay'])).replace('\\n', '<br/>')
    description = str(escape(row['description'])).replace('\\n', '<br/>')

    # required to divide by 1000 bc timestamp is milliseconds from epoch
    # versus seconds
    # ... fucking hell
    timestamp = arrow.get(int(row['timestamp']) / 1000).format('YYYY-MM-DD HH:mm:ss')

    # resolve vote
    c.execute(
        'SELECT rating FROM Votes WHERE app_id=? AND author_email=?',
        (row['mongo_id'], session['email'])
    )
    previousVote = c.fetchone()
    if previousVote is not None:
        previousVote = { 'value': previousVote['rating'] }

    c.execute('SELECT author, author_email, rating FROM Votes WHERE app_id=?', (row['mongo_id'],))
    votes = c.fetchall()

    if len(votes) != 0:
        voteaverage = functools.reduce( lambda a,v: a + v['rating'], votes, 0 ) / len(votes)
        voteaverage = '{:.3}'.format(voteaverage)
    else:
        voteaverage = '?'

    c.execute('''
        SELECT COUNT(a.id) FROM Applicants a
        INNER JOIN Votes v ON a.mongo_id = v.app_id
        WHERE
            v.author_email = ? AND a.completed=1
    ''', (session['email'],))
    flow_votes_completed = c.fetchone()[0]
    c.execute('''
        SELECT COUNT(a.id) FROM Applicants a
        WHERE
            a.completed=1
    ''')
    flow_votes_total = c.fetchone()[0]

    flow_votes_percentage = '{:0.2f}'.format(100 * (flow_votes_completed/flow_votes_total))

    return render_template(
        'dashboard/applicant.html',
        session=session,
        applicant=row,
        timestamp=timestamp,
        essay=essay,
        description=description,
        previousVote=previousVote,
        votes=votes,
        voteaverage=voteaverage,
        flow=request.args.get('flow'),
        flow_votes_completed=flow_votes_completed,
        flow_votes_total=flow_votes_total,
        flow_votes_percentage=flow_votes_percentage
    )

@bp.route('/rate_queue')
@login_required
def rate_queue():
    conn = get_db()

    # this should be cleaner but won't be
    c = conn.cursor()
    c.execute('''
        SELECT name, email, mongo_id
        FROM
            Applicants
        WHERE
            completed=1
        ORDER BY email ASC
    ''')


    rows = c.fetchall()
    targets = []
    done    = []
    for row in rows:
        c.execute('''
            SELECT COUNT(app_id) FROM Votes
            WHERE
                app_id=? AND author_email=?
        ''', (row['mongo_id'],session['email']))
        vcount = c.fetchone()

        if vcount[0] == 0:
            targets.append({
                'name': row['name'],
                'email': row['email'],
                'mongo_id': row['mongo_id']
            })
        else:
            done.append({
                'name': row['name'],
                'email': row['email'],
                'mongo_id': row['mongo_id']
            })

    return render_template(
        'dashboard/rate_queue.html',
        session=session,
        targets=targets,
        done=done
    )

@bp.route('/rate_next')
@login_required
def get_next_applicant():
    # next applicant algorithm:
    # find the next applicant that you haven't yet voted on, and has the least amount of votes

    conn = get_db()
    c = conn.cursor()

    c = conn.cursor()
    c.execute('''
        SELECT mongo_id, email
        FROM
            Applicants
        WHERE
            completed=1
        ORDER BY email ASC
    ''')
    rows = c.fetchall()

    targets = []
    for row in rows:
        c.execute('''
            SELECT COUNT(app_id)
            FROM Votes
            WHERE
                app_id=? AND author_email=?
        ''', (row['mongo_id'], session['email']))
        if c.fetchone()[0] != 0:
            # we've already voted
            continue

        # figure out how many votes are here
        c.execute('''
            SELECT
                COUNT(app_id)
            FROM Votes WHERE
                app_id=?
        ''', (row['mongo_id'],))
        count = c.fetchone()[0]
        targets.append({
            'mongo_id': row['mongo_id'],
            'count': count
        })

    if len(targets) != 0:
        targets.sort(key=lambda k: k['count'])
        return redirect(url_for('dashboard.applicant', mongo_id=targets[0]['mongo_id']) + '?flow=1')
    else:
        flasher('Congrats! You\'ve finished!', color='success')
        return redirect(url_for('dashboard.rate_queue'))


@bp.route('/export')
def export_csv():
    return render_template('dashboard/export.html', session=session)

@bp.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    return response
