from __future__ import absolute_import

import logging
import re
import urlparse
import lxml

import jinja2
import jinja2.ext
import pkg_resources
import pyramid.authorization
import pyramid.config
import pyramid.renderers
import pyramid_jinja2
import pyramid_zodbconn
import venusian
import zc.sourcefactory.source
import zope.app.appsetup.appsetup
import zope.app.appsetup.product
import zope.component
import zope.configuration.xmlconfig
import zope.interface

import zeit.cms.content.sources
import zeit.cms.repository.interfaces
import zeit.cms.repository.repository
import zeit.connector
import zeit.content.article.edit.interfaces
import zeit.cms.interfaces

import zeit.web
import zeit.web.core.cache
import zeit.web.core.interfaces
import zeit.web.core.jinja
import zeit.web.core.repository  # activate monkeypatches
import zeit.web.core.security
import zeit.web.core.solr  # activate monkeypatches
import zeit.web.core.source  # activate monkeypatches

from zeit.content.article.interfaces import IArticle


log = logging.getLogger(__name__)
CONFIG_CACHE = zeit.web.core.cache.get_region('config')


class Application(object):

    DONT_SCAN = [re.compile('test$').search]

    def __init__(self):
        self.settings = Settings()

    def __call__(self, global_config, **settings):
        self.settings.update(settings)
        self.settings['app_servers'] = filter(
            None, settings['app_servers'].split(','))
        self.settings['rewrite_https_links'] = (
            settings.get(
                'transform_to_secure_links_for', 'www.zeit.de')).split(',')
        self.settings['linkreach_host'] = maybe_convert_egg_url(
            settings.get('linkreach_host', ''))
        self.settings['sso_key'] = self.load_sso_key(
            settings.get('sso_key', None))
        # NOTE: non-pyramid utilities can only access deployment settings,
        # runtime settings are not available until Application setup is done.
        zope.component.provideUtility(
            self.settings, zeit.web.core.interfaces.ISettings)
        self.configure()
        return self.config.make_wsgi_app()

    def load_sso_key(self, keyfile):
        if keyfile:
            with open(keyfile[7:], "r") as myfile:
                return myfile.read()

    def configure(self):
        self.configure_zca()
        self.configure_pyramid()

    def configure_pyramid(self):
        log.debug('Configuring Pyramid')

        registry = pyramid.registry.Registry(
            bases=(zope.component.getGlobalSiteManager(),))

        mapper = zeit.web.core.routing.RoutesMapper()
        registry.registerUtility(mapper, pyramid.interfaces.IRoutesMapper)

        self.settings['version'] = pkg_resources.get_distribution(
            'zeit.web').version

        self.config = config = pyramid.config.Configurator(registry=registry)
        config.setup_registry(settings=self.settings)
        # setup_registry() insists on copying the settings mapping into a new
        # `pyramid.config.settings.Settings` instance. But we want
        # registry.settings and the ISettings utility to be the same object,
        # for easy test setup. So we first copy over any values set by Pyramid
        # or included packages, and then replace the Pyramid instance with our
        # instance again.
        self.settings.deployment.update(config.registry.settings)
        config.registry.settings = self.settings

        # Never commit, always abort. zeit.web should never write anything,
        # anyway, and at least when running in preview mode, not committing
        # neatly avoids ConflictErrors.
        self.config.registry.settings['tm.commit_veto'] = lambda *args: True
        self.config.include('pyramid_tm')
        self.configure_jinja()

        if self.settings.get('zodbconn.uri'):
            self.config.include('pyramid_zodbconn')

        config.add_view_predicate(
            'vertical', zeit.web.core.routing.VerticalPredicate,
            weighs_more_than=('custom',))
        config.add_view_predicate(
            'host_restriction', zeit.web.core.routing.HostRestrictionPredicate,
            weighs_more_than=('vertical',))
        config.add_route_predicate(
            'host_restriction', zeit.web.core.routing.HostRestrictionPredicate,
            weighs_more_than=('traverse',))

        config.add_route('lead_story', '/lead-story')
        config.add_route('read_story', '/read-story')
        config.add_route('next_story', '/next-story')
        config.add_route('previous_story', '/previous-story')

        config.set_root_factory(self.get_repository)
        config.scan(package=zeit.talk, ignore=self.DONT_SCAN)

        config.include('pyramid_dogpile_cache2')

        config.set_session_factory(zeit.web.core.session.CacheSession)

        config.set_authentication_policy(
            zeit.web.core.security.AuthenticationPolicy())
        config.set_authorization_policy(
            pyramid.authorization.ACLAuthorizationPolicy())

        return config

    def get_repository(self, request):
        if self.settings.get('zodbconn.uri'):
            connection = pyramid_zodbconn.get_connection(request)
            root = connection.root()
            # We probably should not hardcode the name, but use
            # ZopePublication.root_name instead, but since the name is not ever
            # going to be changed, we can safely skip the dependency on
            # zope.app.publication.
            root_folder = root.get('Application', None)
            zope.component.hooks.setSite(root_folder)
        return zope.component.getUtility(
            zeit.cms.repository.interfaces.IRepository)

    def configure_jinja(self):
        """Sets up names and filters that will be available for all
        templates.
        """
        self.config.include('pyramid_jinja2')
        self.config.add_renderer('.html', pyramid_jinja2.renderer_factory)
        self.config.add_jinja2_extension(
            jinja2.ext.WithExtension)
        self.config.add_jinja2_extension(
            zeit.web.core.jinja.ProfilerExtension)
        self.config.add_jinja2_extension(
            zeit.web.core.jinja.RequireExtension)

        self.config.commit()
        self.jinja_env = env = self.config.get_jinja2_environment()
        env.finalize = zeit.web.core.jinja.finalize
        env.trim_blocks = True
        # Roughly equivalent to `from __future__ import unicode_literals`,
        # see <https://github.com/pallets/jinja/issues/392> and
        # <http://jinja.pocoo.org/docs/2.10/api/#policies>.
        env.policies['compiler.ascii_str'] = False

        default_loader = env.loader
        env.loader = zeit.web.core.jinja.PrefixLoader({
            None: default_loader,
            'dav': zeit.web.core.jinja.HTTPLoader(self.settings.get(
                'load_template_from_dav_url'))
        }, delimiter='://')

        venusian.Scanner(env=env).scan(
            zeit.web.core,
            categories=('jinja',),
            ignore=self.DONT_SCAN)

    def configure_zca(self):
        """Sets up zope.component registrations by reading our
        configure.zcml file.
        """
        log.debug('Configuring ZCA')
        self.configure_product_config()
        zope.component.hooks.setHooks()
        context = zope.configuration.config.ConfigurationMachine()
        zope.configuration.xmlconfig.registerCommonDirectives(context)
        zope.configuration.xmlconfig.include(context, package=zeit.web)
        self.configure_connector(context)
        self.configure_overrides(context)
        context.execute_actions()
        setattr(zope.app.appsetup.appsetup, '__config_context', context)

    def configure_connector(self, context):
        if not self.settings.get('zodbconn.uri'):
            zope.component.provideUtility(
                zeit.cms.repository.repository.Repository(),
                zeit.cms.repository.interfaces.IRepository)
        typ = self.settings['connector_type']
        allowed = ('real', 'dav', 'filesystem', 'mock')
        if typ not in allowed:
            raise ValueError(
                'Invalid setting connector_type=%s, allowed are {%s}'
                % (typ, ', '.join(allowed)))
        zope.configuration.xmlconfig.include(
            context, package=zeit.connector, file='%s-connector.zcml' % typ)

    def configure_product_config(self):
        """Sets values of Zope Product Config used by vivi for configuration,
        using settings from the WSGI ini file.

        Requires the following naming convention in the ini file:
            vivi_<PACKAGE>_<SETTING> = <VALUE>
        for example
            vivi_zeit.connector_repository-path = egg://zeit.web.core/data

        (XXX This is based on the assumption that vivi never uses an underscore
        in a SETTING name.)

        For convenience we resolve egg:// URLs using pkg_resources into file://
        URLs. This functionality should probably move to vivi, see VIV-288.
        """
        for key, value in self.settings.items():
            if not key.startswith('vivi_'):
                continue

            ignored, package, setting = key.split('_')
            if zope.app.appsetup.product.getProductConfiguration(
                    package) is None:
                zope.app.appsetup.product.setProductConfiguration(package, {})
            config = zope.app.appsetup.product.getProductConfiguration(package)
            value = maybe_convert_egg_url(value)
            # XXX Stopgap until FRIED-12, since MockConnector does not
            # understand file-URLs
            if key == 'vivi_zeit.connector_repository-path':
                value = value.replace('file://', '')
            config[setting] = value

    def configure_overrides(self, context):
        """Local development environments use an overrides zcml to allow
        us to mock external dependencies or tweak the zope product config.
        """
        if self.settings.get('mock_solr'):
            zope.configuration.xmlconfig.includeOverrides(
                context, package=zeit.web.core, file='overrides.zcml')


@zope.interface.implementer(zeit.web.core.interfaces.ISettings)
class Settings(pyramid.config.settings.Settings,
               zeit.cms.content.sources.SimpleXMLSourceBase):

    product_configuration = 'zeit.talk'
    config_url = 'runtime-settings-source'

    runtime_config = 'vivi_{}_{}'.format(product_configuration, config_url)

    @property
    def deployment(self):
        return super(Settings, self)

    @property
    def runtime(self):
        if ('backend' not in CONFIG_CACHE.__dict__ or
                not self.deployment.__contains__(self.runtime_config)):
            # We're at an early Application setup stage, no runtime
            # configuration available or needed (e.g. for Pyramid setup).
            return {}
        return self._load_runtime_settings()

    @CONFIG_CACHE.cache_on_arguments()
    def _load_runtime_settings(self):
        result = {}
        for node in self._get_tree().iterfind('setting'):
            result[node.get('name')] = node.pyval
        return result

    @property
    def combined(self):
        result = {}
        result.update(self.runtime)
        result.update(self)
        return result

    def get(self, key, default=None):
        if self.deployment.__contains__(key):
            return self.deployment.get(key)
        return self.runtime.get(key, default)

    def __getitem__(self, key):
        if self.deployment.__contains__(key):
            return self.deployment.get(key)
        return self.runtime[key]

    def __contains__(self, key):
        return (self.deployment.__contains__(key) or
                self.runtime.__contains__(key))

    def keys(self):
        return self.combined.keys()

    def values(self):
        return self.combined.values()

    def items(self):
        return self.combined.items()

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return len(self.keys())


factory = Application()


def maybe_convert_egg_url(url):
    if not url.startswith('egg://'):
        return url
    parts = urlparse.urlparse(url)

    return 'file://' + pkg_resources.resource_filename(
        parts.netloc, parts.path[1:])


def join_url_path(base, path):
    parts = urlparse.urlsplit(base)
    path = (parts.path + path).replace('//', '/')
    return urlparse.urlunsplit(
        (parts[0], parts[1], path, parts[3], parts[4]))


def configure_host(key):
    def wrapped(request):
        conf = zope.component.getUtility(zeit.web.core.interfaces.ISettings)
        prefix = conf.get(key + '_prefix', '')
        version = conf.get('version', 'latest')
        prefix = prefix.format(version=version)
        if not prefix.startswith('http'):
            prefix = join_url_path(
                request.application_url, '/' + prefix.strip('/'))
        return request.route_url('home', _app_url=prefix).rstrip('/')
    wrapped.__name__ = key + '_host'
    return wrapped


class FeatureToggleSource(zeit.cms.content.sources.SimpleContextualXMLSource):
    # Only contextual so we can customize source_class

    product_configuration = 'zeit.web'
    config_url = 'feature-toggle-source'

    class source_class(zc.sourcefactory.source.FactoredContextualSource):

        def find(self, name):
            return self.factory.find(name)

    def find(self, name):
        try:
            return bool(getattr(self._get_tree(), name, False))
        except TypeError:
            return False


FEATURE_TOGGLES = FeatureToggleSource()(None)


def get_teasers(unique_id):
    cp = zeit.cms.interfaces.ICMSContent('http://xml.zeit.de/index')
    regions = [zeit.web.core.centerpage.IRendered(x)
               for x in cp.values() if x.visible]
    for region in regions:
        for area in region.values():
            for teaser in zeit.content.cp.interfaces.ITeaseredContent(area):
                if IArticle in zope.interface.providedBy(teaser):
                    yield teaser

def build_teaser(teaser):
    return {'title': teaser.teaserTitle.strip(),
            'text': teaser.teaserText.strip(),
            'uniqueId': teaser.uniqueId}


@pyramid.view.view_config(
    route_name='lead_story',
    renderer='json')
def get_lead_story(request):
    teaser = next(get_teasers('http://xml.zeit.de/index'))
    return build_teaser(teaser)


@pyramid.view.view_config(
    route_name='next_story',
    renderer='json')
def get_next_story(request):
    uniqueId = request.params.get('uniqueId', None)
    if not uniqueId:
        return get_lead_story(request)

    gen = get_teasers('http://xml.zeit.de/index')
    for teaser in gen:
        if teaser.uniqueId == uniqueId:
            return build_teaser(next(gen))


@pyramid.view.view_config(
    route_name='previous_story',
    renderer='json')
def get_previous_story(request):
    uniqueId = request.params.get('uniqueId', None)
    if not uniqueId:
        return get_lead_story(request)

    gen = get_teasers('http://xml.zeit.de/index')
    prev_teaser = None
    for teaser in gen:
        if teaser.uniqueId == uniqueId:
            return build_teaser(prev_teaser)
        prev_teaser = teaser


@pyramid.view.view_config(
    route_name='read_story',
    renderer='json')
def read_story(request):
    try:
        uniqueId = request.params['uniqueId']
        resource = zeit.cms.interfaces.ICMSContent(uniqueId)
        ssml = body_to_ssml(
            zeit.content.article.edit.interfaces.IEditableBody(
                resource).xml)
        return {'ssml': ssml}
    except:
        # XXX: Needs better error handling.
        return {'ssml': "<speak>Bei der Anfrage trat ein Problem auf</speak>"}


def body_to_ssml(body):
    filter_xslt = lxml.etree.XML("""
        <xsl:stylesheet version="1.0"
            xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
            <xsl:output method="html"
                        omit-xml-declaration="yes" />
          <xsl:template match='/'>
               <speak>
                    <xsl:apply-templates select="//p" />
               </speak>
          </xsl:template>
          <xsl:template match="p">
          <xsl:element name="{name()}">
            <xsl:apply-templates select="*|text()[normalize-space(.) != '']"/>
          </xsl:element>
          </xsl:template>
          <xsl:template match="@*" />
        </xsl:stylesheet>""")
    transform = lxml.etree.XSLT(filter_xslt)
    return lxml.etree.tostring(transform(body))
