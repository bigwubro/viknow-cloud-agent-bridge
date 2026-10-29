// @ts-check
import {themes as prismThemes} from 'prism-react-renderer';

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'ViKnow Developers',
  tagline: 'ViKnow Platform 接入层 API 与运维指南',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'http://36.103.198.236',
  baseUrl: '/',

  organizationName: 'castmeta-research',
  projectName: 'viknow-platform',

  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',

  i18n: {
    defaultLocale: 'zh-Hans',
    locales: ['zh-Hans'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          path: '../docs',
          routeBasePath: '/',
          sidebarPath: './sidebars.js',
          editUrl:
            'https://git.qingxiang.tech:3000/castmeta-research/viknow-platform/src/branch/main/docs/',
          docItemComponent: '@theme/ApiItem',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      }),
    ],
  ],

  plugins: [
    [
      '@easyops-cn/docusaurus-search-local',
      {
        hashed: true,
        language: ['zh', 'en'],
        highlightSearchTermsOnTargetPage: true,
        explicitSearchResultPath: true,
        docsRouteBasePath: ['guide', 'api'],
      },
    ],
    [
      'docusaurus-plugin-openapi-docs',
      {
        id: 'api',
        docsPluginId: 'classic',
        config: {
          viknow_platform: {
            specPath: '../openapi/viknow-platform.json',
            outputDir: '../docs/api',
            sidebarOptions: {
              groupPathsBy: 'tag',
            },
            downloadUrl: '../openapi/viknow-platform.json',
            hideSendButton: false,
          },
        },
      },
    ],
  ],

  themes: ['docusaurus-theme-openapi-docs'],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      image: 'img/docusaurus-social-card.jpg',
      colorMode: {
        defaultMode: 'dark',
        respectPrefersColorScheme: true,
      },
      docs: {
        sidebar: {
          hideable: true,
          autoCollapseCategories: true,
        },
      },
      navbar: {
        title: 'ViKnow',
        style: 'dark',
        logo: {
          alt: 'ViKnow',
          src: 'img/logo.svg',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'guidesSidebar',
            label: '指南',
            position: 'left',
          },
          {
            type: 'docSidebar',
            sidebarId: 'apiSidebar',
            position: 'left',
            label: 'API',
          },
          {
            type: 'search',
            position: 'right',
          },
          {
            href: 'https://git.qingxiang.tech:3000/castmeta-research/viknow-platform',
            label: '源码',
            position: 'right',
            className: 'navbar-github-link',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: '文档',
            items: [
              {label: '快速开始', to: '/guide'},
              {label: 'API 参考', to: '/api/viknow-3-0-api'},
            ],
          },
          {
            title: '仓库',
            items: [
              {
                label: 'Gitea',
                href: 'https://git.qingxiang.tech:3000/castmeta-research/viknow-platform',
              },
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} Castmeta Research.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
        additionalLanguages: ['bash', 'json', 'python'],
      },
      api: {
        authPersistance: 'localStorage',
      },
    }),
};

export default config;
