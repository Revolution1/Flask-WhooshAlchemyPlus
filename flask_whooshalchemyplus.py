'''

    whooshalchemy flask extension
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Adds whoosh indexing capabilities to SQLAlchemy models for Flask
    applications.

    :copyright: (c) 2012 by Karl Gyllstrom
    :license: BSD (see LICENSE.txt)

'''

from __future__ import absolute_import
from __future__ import print_function
from __future__ import with_statement

import heapq
import logging
import os
import sys

import flask_sqlalchemy as flask_sqlalchemy
import sqlalchemy
import whoosh
import whoosh.index
from flask import current_app
from flask_sqlalchemy import DeclarativeMeta
from sqlalchemy.orm.attributes import InstrumentedAttribute
from whoosh.analysis import StemmingAnalyzer
from whoosh.fields import Schema
from whoosh.qparser import AndGroup
from whoosh.qparser import MultifieldParser
from whoosh.qparser import OrGroup

try:
    unicode
except NameError:
    unicode = str

__version__ = '0.7.5'

__searchable__ = '__searchable__'

DEFAULT_WHOOSH_INDEX_NAME = 'whoosh_index'


class _QueryProxy(flask_sqlalchemy.BaseQuery):
    # We're replacing the model's ``query`` field with this proxy. The main
    # thing this proxy does is override the __iter__ method so that results are
    # returned in the order of the whoosh score to reflect text-based ranking.

    def __init__(self, entities, session=None):
        super(_QueryProxy, self).__init__(entities, session)

        self._modelclass = self._mapper_zero().class_
        self._primary_key_name = self._modelclass.whoosh_primary_key
        self._whoosh_searcher = self._modelclass.pure_whoosh

        # Stores whoosh results from query. If ``None``, indicates that no
        # whoosh query was performed.
        self._whoosh_rank = None

    def __iter__(self):
        ''' Reorder ORM-db results according to Whoosh relevance score. '''

        super_iter = super(_QueryProxy, self).__iter__()

        if self._whoosh_rank is None or self._order_by is not False:
            # Whoosh search hasn't been run or caller has explicitly asked
            # for results to be sorted, so behave as normal (no Whoosh
            # relevance score sorting).
            return super_iter

        # Iterate through the values and re-order by whoosh relevance.
        ordered_by_whoosh_rank = []

        super_rows = list(super_iter)
        for row in super_rows:
            # Push items onto heap, where sort value is the rank provided by
            # Whoosh
            if hasattr(row, self._primary_key_name):
                pk = unicode(getattr(row, self._primary_key_name))
                heapq.heappush(ordered_by_whoosh_rank,
                               (self._whoosh_rank[pk], row))
            else:
                return iter(super_rows)

        def _inner():
            while ordered_by_whoosh_rank:
                yield heapq.heappop(ordered_by_whoosh_rank)[1]

        return _inner()

    def whoosh_search(self, query, limit=None,
                      fields=None, or_=False, like=False):
        '''

        Execute text query on database. Results have a text-based
        match to the query, ranked by the scores from the underlying Whoosh
        index.

        By default, the search is executed on all of the indexed fields as an
        OR conjunction. For example, if a model has 'title' and 'content'
        indicated as ``__searchable__``, a query will be checked against both
        fields, returning any instance whose title or content are a content
        match for the query. To specify particular fields to be checked,
        populate the ``fields`` parameter with the desired fields.

        By default, results will only be returned if they contain all of the
        query terms (AND). To switch to an OR grouping, set the ``or_``
        parameter to ``True``.

        '''
        if not query:
            return self.filter(sqlalchemy.text('null'))
        if not isinstance(query, unicode):
            query = unicode(query)

        results = self._whoosh_searcher(query, limit, fields, or_)

        result_set = set()
        result_ranks = {}

        for rank, result in enumerate(results):
            pk = result[self._primary_key_name]
            result_set.add(pk)
            result_ranks[pk] = rank

        length = len(result_set)
        like_limit = limit - length if limit else None
        if like and like_limit is not 0:
            # if True, will return whoosh search result
            # and fuzzy search result appended behind using SQL LIKE
            query_colums = []
            if fields is None:
                fields = self._whoosh_searcher._index.schema._fields.keys()
            for clm in set(fields) - {self._primary_key_name}:
                attr = getattr(self._modelclass, clm)
                if isinstance(attr, InstrumentedAttribute):
                    query_colums.append(attr.like(u'%{}%'.format(query)))
            id_tuples = self.filter(sqlalchemy.or_(*query_colums)) \
                .with_entities(self._primary_key_name).all()
            ids = [unicode(i[0]) for i in id_tuples]
            ids = sorted(set(ids) - result_set)
            if ids:
                for rank, pk in enumerate(ids[:like_limit]):
                    result_set.add(pk)
                    result_ranks[pk] = length + rank

        if not result_set:
            # We don't want to proceed with empty results because we get a
            # stderr warning from sqlalchemy when executing 'in_' on empty set.
            # However we cannot just return an empty list because it will not
            # be a query.

            # XXX is this efficient?
            return self.filter(sqlalchemy.text('null'))

        f = self.filter(getattr(self._modelclass,
                                self._primary_key_name).in_(result_set))

        f._whoosh_rank = result_ranks

        return f


class _Searcher(object):
    ''' Assigned to a Model class as ``pure_search``, which enables
    text-querying to whoosh hit list. Also used by ``query.whoosh_search``'''

    def __init__(self, primary, index):
        self.primary_key_name = primary
        self._index = index
        self.searcher = index.searcher()
        self._all_fields = list(set(index.schema._fields.keys()) -
                                set([self.primary_key_name]))

    def __call__(self, query, limit=None, fields=None, or_=False):
        if fields is None:
            fields = self._all_fields

        group = OrGroup if or_ else AndGroup
        parser = MultifieldParser(fields, self._index.schema, group=group)
        return self._index.searcher().search(parser.parse(query),
                                             limit=limit)


def whoosh_index(app, model):
    ''' Create whoosh index for ``model``, if one does not exist. If
    the index exists it is opened and cached. '''

    # gets the whoosh index for this model, creating one if it does not exist.
    # A dict of model -> whoosh index is added to the ``app`` variable.

    if app.config.get('WHOOSH_DISABLED') is True:
        logging.info('Whoosh has been disabled!')
        return
    if not hasattr(app, 'whoosh_indexes'):
        app.whoosh_indexes = {}
    if not hasattr(app, 'whoosh_models'):
        app.whoosh_models = {}

    return app.whoosh_indexes.get(model.__name__,
                                  _create_index(app, model))


def _get_analyzer(app, model):
    analyzer = getattr(model, '__analyzer__', None)

    if not analyzer and app.config.get('WHOOSH_ANALYZER'):
        analyzer = app.config['WHOOSH_ANALYZER']

    if not analyzer:
        analyzer = StemmingAnalyzer()

    return analyzer


def _create_index(app, model):
    # a schema is created based on the fields of the model. Currently we only
    # support primary key -> whoosh.ID, and sqlalchemy.(String, Unicode, Text)
    # -> whoosh.TEXT.

    if not app.config.get('WHOOSH_BASE'):
        # XXX todo: is there a better approach to handle the absenSe of a
        # config value for whoosh base? Should we throw an exception? If
        # so, this exception will be thrown in the after_commit function,
        # which is probably not ideal.

        app.config['WHOOSH_BASE'] = DEFAULT_WHOOSH_INDEX_NAME

    # we index per model.
    wi = os.path.join(app.config.get('WHOOSH_BASE'),
                      model.__name__)

    analyzer = _get_analyzer(app, model)
    schema, primary_key = _get_whoosh_schema_and_primary_key(model, analyzer)

    if whoosh.index.exists_in(wi):
        index = whoosh.index.open_dir(wi)
    else:
        if not os.path.exists(wi):
            os.makedirs(wi)
        index = whoosh.index.create_in(wi, schema)

    app.whoosh_indexes[model.__name__] = index
    app.whoosh_models[model.__name__] = model
    model.pure_whoosh = _Searcher(primary_key, index)
    model.whoosh_primary_key = primary_key

    # change the query class of this model to our own
    if model.query_class is not flask_sqlalchemy.BaseQuery \
            and model.query_class is not _QueryProxy:
        print(model.query_class, _QueryProxy)
        model.query_class = type(
            'MultipliedQuery', (model.query_class, _QueryProxy), {}
        )
    else:
        model.query_class = _QueryProxy

    return index


def _get_whoosh_schema_and_primary_key(model, analyzer):
    schema = {}
    primary = None
    searchable = set(model.__searchable__)
    columns = model.__table__.columns
    parent_columns = model.__base__.__table__.columns if hasattr(
        model.__base__, '__table__') else []
    for field in columns:
        if field.primary_key:
            schema[field.name] = whoosh.fields.ID(stored=True, unique=True)
            primary = field.name
    for name in searchable:
        try:
            if name in columns:
                attr = columns.get(name).type
            elif name in parent_columns:
                attr = parent_columns.get(name).type
            else:
                attr = getattr(model, name)
            if isinstance(attr, (sqlalchemy.types.Text,
                                 sqlalchemy.types.String,
                                 sqlalchemy.types.Unicode,
                                 property)):
                schema[name] = whoosh.fields.TEXT(
                    analyzer=analyzer, vector=True)
        except AttributeError:
            logging.warning('{0} does not have {1} field {2}'
                            .format(model.__name__, __searchable__, name))
    return Schema(**schema), primary


@flask_sqlalchemy.models_committed.connect
def _after_flush(app, changes):
    # Any db updates go through here. We check if any of these models have
    # ``__searchable__`` fields, indicating they need to be indexed. With these
    # we update the whoosh index for the model. If no index exists, it will be
    # created here; this could impose a penalty on the initial commit of a
    # model.
    if app.config.get('WHOOSH_DISABLED') is True:
        return
    bytype = {}  # sort changes by type so we can use per-model writer
    for change in changes:
        update = change[1] in ('update', 'insert')

        if hasattr(change[0].__class__, __searchable__):
            bytype.setdefault(change[0].__class__.__name__, []).append(
                (update, change[0]))
    if not bytype:
        return
    try:
        for model, values in bytype.items():
            index = whoosh_index(app, values[0][1].__class__)
            with index.writer() as writer:
                for update, v in values:
                    has_parent = isinstance(
                        v.__class__.__base__, DeclarativeMeta) and \
                                 hasattr(v.__class__.__base__, '__searchable__')
                    index_one_record(
                        v, not update, writer, index_parent=has_parent)
    except Exception as ex:
        logging.error("FAIL updating index of %s msg: %s" % (model, str(ex)))


def index_one_record(record, delete=False, writer=None, index_parent=False):
    index = whoosh_index(current_app, record.__class__)
    close = False
    if not writer:
        writer = index.writer()
        close = True
    if index_parent:
        # index parent class
        parent_writer = whoosh_index(
            current_app, record.__class__.__base__).writer()
    primary_field = record.pure_whoosh.primary_key_name
    searchable = index.schema.names()
    if not delete:
        attrs = {}
        for key in searchable:
            attrs[key] = unicode(getattr(record, key))
        attrs[primary_field] = unicode(
            getattr(record, primary_field))
        writer.update_document(**attrs)
        if index_parent:
            parent_writer.update_document(**attrs)
    else:
        writer.delete_by_term(
            primary_field, unicode(getattr(record, primary_field)))
        if index_parent:
            parent_writer.delete_by_term(
                primary_field, unicode(getattr(record, primary_field)))
    if close:
        writer.commit()


def index_one_model(model):
    index = whoosh_index(current_app, model)
    with index.writer() as writer:
        all_model = model.query.enable_eagerloads(False).yield_per(100)
        for record in all_model:
            index_one_record(record, writer=writer)


def whoosh_index_all(app):
    """
    app -> [indexes]
    """
    all_models = app.extensions[
        'sqlalchemy'].db.Model._decl_class_registry.values()
    models = [i for i in all_models if hasattr(i, '__searchable__')]
    return [(m, whoosh_index(app, m)) for m in models]


def index_all(app):
    """
    Index all records in database.
    """
    from datetime import datetime
    start = datetime.now()
    for model, _ in whoosh_index_all(app):
        # import wdb; wdb.set_trace()
        print("Indexing %s...%s" %
              (model.__name__, ' ' * (25 - len(model.__name__))), end='')
        sys.stdout.flush()
        before = datetime.now()
        index_one_model(model)
        print("done\t%ss" % (datetime.now() - before).seconds)
    print(' ' * 37 + 'total\t%ss' % (datetime.now() - start).seconds)


class WhooshDisabled(object):
    """
    Disable whoosh indexing temporarily

    usage:
    ::
        with WhooshDisabled():
            do sth.
    """

    def __init__(self):
        self.app = current_app
        self._default_state = self._get_default_state()

    def _get_default_state(self):
        return self.app.config.get('WHOOSH_DISABLED', False)

    def __enter__(self):
        self.app.config['WHOOSH_DISABLED'] = True

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.app.config['WHOOSH_DISABLED'] = self._default_state


def init_app(app):
    app.config.setdefault('WHOOSH_DISABLED', False)
    if app.config['WHOOSH_DISABLED']:
        flask_sqlalchemy.models_committed.disconnect(_after_flush)
    else:
        whoosh_index_all(app)
