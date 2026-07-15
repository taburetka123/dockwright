fun handle(event: ChatEvent) {
    val chat = chatDao.find(event.chatId)
        ?: throw ChatNotFoundException(event.chatId)   // line from the ticket
    chat.apply(event)                                   // ~45s p99 (PDF render)
    chatDao.save(chat)
}
