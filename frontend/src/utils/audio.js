// small audio utility using Web Audio API
export function beep(frequency = 880, duration = 0.12, type = 'sine'){
  try{
    const AudioCtx = window.AudioContext || window.webkitAudioContext
    const ctx = new AudioCtx()
    const o = ctx.createOscillator()
    const g = ctx.createGain()
    o.type = type
    o.frequency.value = frequency
    o.connect(g)
    g.connect(ctx.destination)
    const now = ctx.currentTime
    g.gain.setValueAtTime(0.0001, now)
    g.gain.exponentialRampToValueAtTime(0.5, now + 0.01)
    o.start(now)
    g.gain.exponentialRampToValueAtTime(0.0001, now + duration)
    setTimeout(()=>{ try{ o.stop(); ctx.close() }catch(e){} }, (duration + 0.05) * 1000)
  }catch(err){
    // ignore if audio not available
    console.warn('beep failed', err)
  }
}
