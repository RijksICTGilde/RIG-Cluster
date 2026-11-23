<#import "template.ftl" as layout>
<@layout.emailLayout>
<p>${msg("zadMagicLinkBodyIntro")}</p>
<p>${msg("zadMagicLinkBodyCode")} <strong style="font-size: 1.5em; letter-spacing: 0.2em;">${code}</strong></p>
<p>${msg("zadMagicLinkBodyValid", validMinutes)}</p>
<p>${msg("zadMagicLinkBodyLink")}<br/><a href="${link}">${link}</a></p>
<p>${msg("zadMagicLinkBodyNotYou")}</p>
</@layout.emailLayout>
