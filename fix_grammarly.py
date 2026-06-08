content = open('/app/build/index.html').read()
content = content.replace('<body data-sveltekit-preload-data="hover">', '<body data-sveltekit-preload-data="hover" data-gramm="false" data-gramm_editor="false" data-enable-grammarly="false">')
open('/app/build/index.html', 'w').write(content)
