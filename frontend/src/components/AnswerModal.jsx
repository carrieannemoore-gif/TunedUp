import React, { useState } from 'react'

export default function AnswerModal({ onClose, onSubmit, strictMode }){
  const [title, setTitle] = useState('')
  const [artist, setArtist] = useState('')

  return (
    <div style={{position:'fixed',left:0,top:0,right:0,bottom:0,display:'flex',alignItems:'center',justifyContent:'center',background:'rgba(0,0,0,0.4)'}}>
      <div style={{background:'white',padding:20,borderRadius:8,width:520}}>
        <h3>Submit Answer</h3>
        <div style={{display:'flex',gap:8,marginBottom:8}}>
          <input placeholder='Song title' value={title} onChange={e=>setTitle(e.target.value)} style={{flex:1,padding:8}} />
          {strictMode && <input placeholder='Artist' value={artist} onChange={e=>setArtist(e.target.value)} style={{flex:1,padding:8}} />}
        </div>
        <div style={{display:'flex',justifyContent:'flex-end',gap:8}}>
          <button onClick={onClose}>Cancel</button>
          <button onClick={()=>onSubmit({title,artist}, true)}>Mark Correct</button>
          <button onClick={()=>onSubmit({title,artist}, false)}>Mark Wrong</button>
        </div>
      </div>
    </div>
  )
}
