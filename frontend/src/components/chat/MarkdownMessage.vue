<template>
  <div v-if="ready" class="markdown-body" v-html="rendered"></div>
  <div v-else class="chat-message-text">{{ content }}</div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ content: string }>()

interface MarkdownRenderer {
  render: (source: string) => string
}

let markdownRendererPromise: Promise<MarkdownRenderer> | null = null
let markdownRendererLoaded = false
let sanitizerHookInstalled = false

async function loadMarkdownRenderer(): Promise<MarkdownRenderer> {
  if (!markdownRendererPromise) {
    markdownRendererPromise = Promise.all([
      import('dompurify'),
      import('marked'),
      import('marked-highlight'),
      import('highlight.js/lib/core'),
      import('highlight.js/lib/languages/plaintext'),
      import('highlight.js/lib/languages/bash'),
      import('highlight.js/lib/languages/javascript'),
      import('highlight.js/lib/languages/typescript'),
      import('highlight.js/lib/languages/json'),
      import('highlight.js/lib/languages/python'),
      import('highlight.js/lib/languages/sql'),
      import('highlight.js/lib/languages/xml'),
      import('highlight.js/lib/languages/css'),
      import('highlight.js/lib/languages/markdown'),
      import('highlight.js/lib/languages/yaml'),
    ]).then(([
      domPurifyModule,
      markedModule,
      markedHighlightModule,
      hljsModule,
      plaintextModule,
      bashModule,
      javascriptModule,
      typescriptModule,
      jsonModule,
      pythonModule,
      sqlModule,
      xmlModule,
      cssModule,
      markdownModule,
      yamlModule,
    ]) => {
      const DOMPurify = domPurifyModule.default
      const { Marked } = markedModule
      const { markedHighlight } = markedHighlightModule
      const hljs = hljsModule.default

      function registerLanguage(name: string, language: Parameters<typeof hljs.registerLanguage>[1]) {
        if (!hljs.getLanguage(name)) {
          hljs.registerLanguage(name, language)
        }
      }

      registerLanguage('plaintext', plaintextModule.default)
      registerLanguage('text', plaintextModule.default)
      registerLanguage('bash', bashModule.default)
      registerLanguage('shell', bashModule.default)
      registerLanguage('sh', bashModule.default)
      registerLanguage('javascript', javascriptModule.default)
      registerLanguage('js', javascriptModule.default)
      registerLanguage('typescript', typescriptModule.default)
      registerLanguage('ts', typescriptModule.default)
      registerLanguage('json', jsonModule.default)
      registerLanguage('python', pythonModule.default)
      registerLanguage('py', pythonModule.default)
      registerLanguage('sql', sqlModule.default)
      registerLanguage('xml', xmlModule.default)
      registerLanguage('html', xmlModule.default)
      registerLanguage('vue', xmlModule.default)
      registerLanguage('css', cssModule.default)
      registerLanguage('markdown', markdownModule.default)
      registerLanguage('md', markdownModule.default)
      registerLanguage('yaml', yamlModule.default)
      registerLanguage('yml', yamlModule.default)

      const marked = new Marked(
        markedHighlight({
          langPrefix: 'hljs language-',
          highlight(code, lang) {
            const language = lang && hljs.getLanguage(lang) ? lang : 'plaintext'
            try {
              return hljs.highlight(code, { language, ignoreIllegals: true }).value
            } catch {
              return code
            }
          },
        }),
      )
      marked.setOptions({ breaks: true, gfm: true })

      if (typeof window !== 'undefined' && !sanitizerHookInstalled) {
        DOMPurify.addHook('afterSanitizeAttributes', (node) => {
          if (node.tagName === 'A') {
            node.setAttribute('target', '_blank')
            node.setAttribute('rel', 'noopener noreferrer')
          }
        })
        sanitizerHookInstalled = true
      }

      markdownRendererLoaded = true

      return {
        render(source: string) {
          const html = marked.parse(source) as string
          return DOMPurify.sanitize(html)
        },
      }
    })
  }

  return markdownRendererPromise
}

const rendered = ref('')
const ready = ref(markdownRendererLoaded)
let renderVersion = 0

watch(
  () => props.content,
  async (value) => {
    const source = value ?? ''
    if (!source) {
      rendered.value = ''
      ready.value = true
      return
    }

    const currentVersion = ++renderVersion
    if (!markdownRendererLoaded) {
      ready.value = false
    }

    const renderer = await loadMarkdownRenderer()
    if (currentVersion !== renderVersion) return

    rendered.value = renderer.render(source)
    ready.value = true
  },
  { immediate: true },
)
</script>
